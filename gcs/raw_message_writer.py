"""
Per-connection "Raw" data-file writer for CopterSonde GCS.

Writes the RAW data file into a ``Raw`` folder inside a ``Messages`` folder
that always sits next to the telemetry log's ``TelemetryLog`` folder, with
the same platform-dependent placement (built-in storage on desktop, the
micro SD card on Android -- see gcs.storage_paths and gcs.tlog_writer).

File contents follow the revised Raw spec (SoW 205192-11 section 2.1 /
Table 2-1): 35 columns.  Per section 2.1 the file's naming, the events that
open and close it, the directory, and the line format are all *unchanged*
from the original Raw file (SoW 205174 section 1.6); only the column contents
are brought up to Table 2-1 here.

Line format (unchanged):
  * UTF-8 encoding; DOS (CRLF) line endings on every line.
  * Two header lines: row 1 = column names, row 2 = column units.
  * Comma-delimited columns, in the order given by RAW_COLUMNS.
  * One data row per balanced line, written while the aircraft is ARMED
    (SoW 205174 section 1.6 / 205192 section 5 -- the raw file covers the whole
    armed period, not just ascents; the client gates on the armed flag carried
    on the balanced line).  Values use the trailing precision the SoW mandates
    per column.
  * Filename ``RAW_MM-DD-YYYY_HH-MM-SS.csv`` where the date/time is the UTC
    system time at file creation (the .csv extension follows SoW 205174 rev 14
    addendum 2.2, which changed the Raw output from .txt to .csv).  A fresh file
    is created for each viable vehicle connection (open() runs when the first
    message arrives).

Columns 1-21 are the balanced live data.  Columns 22-35 are new in Table 2-1:
Satellites and HDOP (columns 34-35) are sourced live from GPS_RAW_INT (msg 24),
carried forward on the balanced line by the balancer, and Drone Serial Number
comes from the Remote ID settings; every other new column is a placeholder that
emits its Table 2-1 default until its source is plumbed -- the "File" columns
(Home Position, the Ground-station readings) from the operator-input file (SoW
205192-11 section 3), and Data Quality / Powered Age / Armed Age in a later
block.

The header lines and every data row are generated from the SAME RAW_COLUMNS
table, so the columns can never drift apart: each column carries its name,
its unit, the format string for its value, and a getter that pulls (and
unit-converts) that value from a BalancedLine.

Lifecycle mirrors TlogWriter: open() on first-message arrival (MAVLink IO
thread), write_row() per balanced line while armed (IO thread), close() on
disconnect (main/UI thread).
"""

import math
import os
import threading
from collections import namedtuple
from datetime import datetime, timezone

from gcs.logutil import get_logger
from gcs.storage_paths import resolve_base

log = get_logger("raw_msg_writer")

# One column of the Raw file:
#   name  -- header line 1
#   unit  -- header line 2
#   fmt   -- str.format spec for the value (trailing precision per SoW Table 2-1)
#   get   -- callable(BalancedLine) -> value, in the column's display units
Column = namedtuple("Column", ["name", "unit", "fmt", "get"])

# Raw file columns, in order (SoW 205192-11 Table 2-1).  BalancedLine carries
# canonical SI-ish units (K, rad, rad/s, hPa, deg, m, m/s); the getters apply
# the final display conversions -- Kelvin -> Celsius and radians -> degrees --
# here.  Trailing precision matches Table 2-1 ("only trailing precision is
# required to be identical").
#
# Two content changes from the old 205174 Table 1-3 within columns 1-21:
#   * column 2's header is now "Altitude ASL" (was "ASL");
#   * Latitude/Longitude report 6 dp (Table 2-1 "12.345678"), matching the
#     ALM/TIM, where the old Raw file reported raw degreesE7 precision (7 dp).
RAW_COLUMNS = [
    Column("Time",           "s",     "{:.2f}", lambda L: L.time),
    Column("Altitude ASL",   "m",     "{:.1f}", lambda L: L.alt_asl),
    Column("Pressure",       "mB",    "{:.2f}", lambda L: L.pressure),
    Column("Air Temp A",     "C",     "{:.2f}", lambda L: L.temps[0] - 273.15),
    Column("Air Temp B",     "C",     "{:.2f}", lambda L: L.temps[1] - 273.15),
    Column("Air Temp C",     "C",     "{:.2f}", lambda L: L.temps[2] - 273.15),
    Column("Rel Hum A",      "%",     "{:.1f}", lambda L: L.humidity[0]),
    Column("Rel Hum B",      "%",     "{:.1f}", lambda L: L.humidity[1]),
    Column("Rel Hum C",      "%",     "{:.1f}", lambda L: L.humidity[2]),
    Column("Latitude",       "deg",   "{:.6f}", lambda L: L.lat),
    Column("Longitude",      "deg",   "{:.6f}", lambda L: L.lon),
    Column("Roll",           "deg",   "{:.1f}", lambda L: math.degrees(L.roll)),
    Column("Roll Rate",      "deg/s", "{:.1f}", lambda L: math.degrees(L.rollspeed)),
    Column("Pitch",          "deg",   "{:.1f}", lambda L: math.degrees(L.pitch)),
    Column("Pitch Rate",     "deg/s", "{:.1f}", lambda L: math.degrees(L.pitchspeed)),
    Column("Yaw",            "deg",   "{:.1f}", lambda L: math.degrees(L.yaw)),
    Column("Yaw Rate",       "deg/s", "{:.1f}", lambda L: math.degrees(L.yawspeed)),
    Column("Velocity North", "m/s",   "{:.2f}", lambda L: L.vx),
    Column("Velocity East",  "m/s",   "{:.2f}", lambda L: L.vy),
    Column("Velocity Down",  "m/s",   "{:.2f}", lambda L: L.vz),
    Column("Custom Mode",    "N/A",   "{:d}",   lambda L: int(L.custom_mode)),
    # -- Columns 22-35: new in Table 2-1. ------------------------------------
    # Satellites/HDOP (34-35) are live from GPS_RAW_INT (msg 24), carried on
    # the balanced line.  HDOP is eph/100 (the balancer already does the /100
    # and caps unknown at 99.99).  Drone Serial Number is writer-supplied from
    # the Remote ID settings (getter None -- see write_row).  Every other new
    # column is a placeholder emitting its Table 2-1 "Default Value if Optional"
    # (0) at the column's reporting precision -- the same convention the ALM/TIM
    # constants block uses for the not-yet-sourced operator-file values.  The
    # remaining "File" placeholders (Home Position, Ground-station readings) get
    # their real values once the operator-input file (section 3) is read; Data
    # Quality and Powered/Armed Age have no source until a later block.
    Column("Data Quality",            "N/A", "{:d}",   lambda L: 0),
    Column("Home Position Latitude",  "deg", "{:.6f}", lambda L: 0.0),
    Column("Home Position Longitude", "deg", "{:.6f}", lambda L: 0.0),
    Column("Home Position Altitude",  "m",   "{:.1f}", lambda L: 0.0),
    Column("Drone Serial Number",     "N/A", "{}",     None),   # writer-supplied; see write_row
    Column("Ground Wind Speed",       "m/s", "{:.1f}", lambda L: 0.0),
    Column("Ground Wind Direction",   "deg", "{:d}",   lambda L: 0),
    Column("Ground Air Temperature",  "C",   "{:.2f}", lambda L: 0.0),
    Column("Ground Humidity",         "%",   "{:.1f}", lambda L: 0.0),
    Column("Ground Pressure",         "mB",  "{:.2f}", lambda L: 0.0),
    Column("Powered Age",             "s",   "{:d}",   lambda L: 0),
    Column("Armed Age",               "s",   "{:d}",   lambda L: 0),
    Column("Satellites",              "N/A", "{:d}",   lambda L: int(L.satellites)),
    Column("HDOP",                    "N/A", "{:.1f}", lambda L: L.hdop),
]

# Filename: RAW_MM-DD-YYYY_HH-MM-SS.csv (UTC system time at creation).
_FILENAME_TIME_FORMAT = "RAW_%m-%d-%Y_%H-%M-%S"

# DOS line ending mandated by the SoW.  The file is opened with newline=""
# so this is written verbatim on every platform -- including the Android /
# HereLink target, where a default text-mode write would emit a bare LF.
_EOL = "\r\n"

# Two-line header: comma-delimited column names, then comma-delimited units.
_HEADER = (
    ",".join(c.name for c in RAW_COLUMNS) + _EOL
    + ",".join(c.unit for c in RAW_COLUMNS) + _EOL
)


def _default_log_dir():
    """Return the ``Messages/Raw`` directory, always a sibling of TelemetryLog.

    We resolve the telemetry log's directory exactly the way TlogWriter does
    (``resolve_base("TelemetryLog", prefer_removable=True)``) and then place
    ``Messages/Raw`` next to it.  Deriving from the resolved TelemetryLog path
    -- rather than calling ``resolve_base("Messages/Raw")`` independently --
    guarantees the two folders share the same parent on every platform, even
    in the corner cases where independent resolution could diverge (e.g. which
    volume wins on Android when an SD card is present).
    """
    telemetry_dir = resolve_base("TelemetryLog", prefer_removable=True)
    base = os.path.dirname(telemetry_dir)
    return os.path.join(base, "Messages", "Raw")


class RawMessageWriter:
    """Writes one Raw data file per connection.

    Thread-safety mirrors TlogWriter/MessageLogger: open() and write_row() run
    on the MAVLink IO thread (the file is opened lazily when the first message
    arrives), while close() runs on the main (UI) thread from stop().  A lock
    guards the file handle so a write can never race with a close.
    """

    def __init__(self, log_dir=None):
        self._dir = log_dir or _default_log_dir()
        self._fh = None
        self._path = None
        self._count = 0
        self._serial = "0"
        self._lock = threading.Lock()

    @property
    def path(self):
        return self._path

    def open(self, serial="0", start_time=None):
        """Create a fresh ``RAW_<UTC timestamp>.csv`` and write the header.

        Called when the first MAVLink message of a connection arrives (from
        the client's ``_open_telemetry_log`` hook), so the file appears at the
        same instant as the .tlog.  The two header lines are written and
        flushed here so a well-formed file exists on disk immediately, even
        before any data row is written.  ``serial`` is the drone serial number
        from the Remote ID settings, emitted in every row's Drone Serial Number
        column; it falls back to "0" when empty.  ``start_time`` is the first
        message's UNIX time -- the filename is stamped from it so a replay names
        its file from the recording rather than wall-clock; it falls back to the
        current time when not given.
        """
        # Defensive: close anything left open by a prior connection.
        self.close()
        self._serial = serial or "0"
        try:
            os.makedirs(self._dir, exist_ok=True)
            # UTC of the first message (the recorded time on replay), per the SoW.
            when = (datetime.fromtimestamp(start_time, timezone.utc)
                    if start_time is not None else datetime.now(timezone.utc))
            stamp = when.strftime(_FILENAME_TIME_FORMAT)
            path = os.path.join(self._dir, f"{stamp}.csv")
            # Don't clobber if two connections land in the same second
            # (mirrors TlogWriter / MessageLogger).
            n = 1
            while os.path.exists(path):
                path = os.path.join(self._dir, f"{stamp}_{n}.csv")
                n += 1
            with self._lock:
                # newline="" => no newline translation; we emit explicit CRLF.
                self._fh = open(path, "w", encoding="utf-8", newline="")
                self._path = path
                self._count = 0
                self._fh.write(_HEADER)
                self._fh.flush()
            log.info("Raw data file opened: %s", path)
        except Exception:
            log.exception("Failed to open raw data file")
            with self._lock:
                self._fh = None
                self._path = None

    def write_row(self, line):
        """Append one balanced sample as a CSV data row (SoW Table 2-1).

        The client calls this for every balanced line while the aircraft is
        armed (SoW 205174 section 1.6 / 205192 section 5), so a file's data rows
        cover its whole armed period -- not just the ascents.  The row is built
        from the same RAW_COLUMNS table that produced the header, so the two
        cannot drift.

        A no-op when the file isn't open -- e.g. during a replay, where the
        client's open() hook is skipped -- so the caller never has to check.
        """
        # Build outside the lock: the only writer state read here is the serial,
        # set at open() on this same IO thread.  A column whose getter is None is
        # writer-supplied (the file-level serial), not derived from the line.
        row = ",".join(
            col.fmt.format(self._serial if col.get is None else col.get(line))
            for col in RAW_COLUMNS
        ) + _EOL
        with self._lock:
            if self._fh is None:
                return
            try:
                self._fh.write(row)
                self._fh.flush()
                self._count += 1
            except Exception:
                log.exception("Failed to write raw data row")

    def close(self):
        """Flush and close the current file (if open)."""
        with self._lock:
            if self._fh is None:
                return
            try:
                self._fh.flush()
                self._fh.close()
                log.info("Raw data file closed: %s (%d data rows)",
                         self._path, self._count)
            except Exception:
                log.exception("Failed to close raw data file")
            finally:
                self._fh = None
                self._path = None
                self._count = 0
