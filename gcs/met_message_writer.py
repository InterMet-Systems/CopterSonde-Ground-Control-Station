"""
Shared base for CopterSonde's per-ascent CSV message writers (ALM and TIM).

Both messages are, structurally, the same file (SoW 205192-11 sections 2.2 and
2.3): a block of CONSTANT data at the top (Table 2-3; "the constants are
identical to ALM"), then time-varying data -- two header lines (names, units)
followed by one row per emitted (binned) LevelRecord, comma-delimited, CRLF,
UTF-8.  A fresh file is opened per ascent, named
``<PREFIX>_<SRN>_<YYYYMMDD>_<HHmmss>[_<string>].csv`` (the operator string and
its leading underscore dropped when empty), in ``Messages/<SUBDIR>`` next to the
telemetry log.

Everything that is identical between the two messages lives here: the constants
block, the filename + directory rules, the open/write/close lifecycle, the
thread-safe file handle, and the data row mechanism (header and every row are
generated from the SAME per-subclass column table, so they can never drift).

A subclass supplies only what actually differs:
  * ``PREFIX``  -- filename prefix ("ALM" / "TIM")
  * ``SUBDIR``  -- Messages subfolder ("AltitudeLevel" / "TimeInterval")
  * ``COLUMNS`` -- the Table 2-4 / Table 2-6 column list (a different order and,
                   for a few columns, different getters)
  * ``LOG_NAME``-- logger name
  * ``_origin(record)`` (optional) -- the per-row time used as the origin for
    the "Time Since Start" column.  The default is the bin-mean sample time
    (ALM); TIM overrides it to the bin's start second.

A column is ``Column(name, unit, fmt, get)`` where ``get`` is
``callable(record, t0) -> value`` in the column's display units, ``fmt`` is the
SoW trailing precision (``"{}"`` passes a preformatted string through), and
``t0`` is this file's first-row origin (for "Time Since Start").
"""

import os
import threading
from collections import namedtuple
from datetime import datetime, timezone

from gcs.logutil import get_logger
from gcs.met_derive import wind_speed_dir
from gcs.storage_paths import output_dirs, tee_open

# DOS line ending mandated by the SoW.  Files are opened with newline="" so this
# is written verbatim on every platform (incl. the Android / HereLink target).
_EOL = "\r\n"

# Message version reported in the constants block (SoW Table 2-3 row 1; "the
# message version at completion should be 1.0.0 for both ALM and TIM").
_MESSAGE_VERSION = "1.0.0"

Column = namedtuple("Column", ["name", "unit", "fmt", "get"])


def _utc(ts):
    """UTC datetime for a UNIX-seconds timestamp (tz-aware; no deprecation)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _wind_dir_int(rec):
    """Integer wind direction in [0, 360), rounded to nearest.

    Rounded rather than truncated -- truncation would bias every reading low --
    and 360 wraps back to 0.  Shared by both messages' Wind Direction column.
    """
    return int(round(wind_speed_dir(rec)[1])) % 360


# ----------------------------------------------------------------------------
# Constant data block (SoW Table 2-3), identical for ALM and TIM.  Unix Start
# Time, Raw Data Filename, and Drone Serial Number have a source; the remaining
# operator-input-file constants and the SoW placeholders default to 0 / empty,
# each formatted at its SoW reporting precision.  Each row is a triplet
# ``name,value,unit`` -- the unit (and its leading comma) dropped when N/A, but
# every row terminated with a trailing comma to match the SoW's example.
# ----------------------------------------------------------------------------
def _constant_rows(unix_start_time, raw_filename, serial):
    """Return the constants block as (name, value_str, unit_or_None) triplets."""
    return [
        ("Message Version",         _MESSAGE_VERSION,           None),
        ("Drone Serial Number",     serial,                     None),
        ("Drone Powered Age",       "0",                        "s"),    # placeholder
        ("Drone Armed Age",         "0",                        "s"),    # placeholder
        ("Raw Data Filename",       raw_filename,               None),
        ("Unix Start Time",         str(int(unix_start_time)),  "s"),
        ("Ground Wind Speed",       "{:.1f}".format(0.0),       "m/s"),  # operator file (TBD)
        ("Ground Wind Direction",   "0",                        "deg"),  # operator file (TBD)
        ("Ground Air Temperature",  "{:.2f}".format(0.0),       "C"),    # operator file (TBD)
        ("Ground Humidity",         "{:.1f}".format(0.0),       "%"),    # operator file (TBD)
        ("Ground Pressure",         "{:.2f}".format(0.0),       "mB"),   # operator file (TBD)
        ("Home Position Latitude",  "{:.6f}".format(0.0),       "deg"),  # operator file (TBD)
        ("Home Position Longitude", "{:.6f}".format(0.0),       "deg"),  # operator file (TBD)
        ("Home Position Altitude",  "{:.1f}".format(0.0),       "m"),    # operator file (TBD)
    ]


def _constants_block(unix_start_time, raw_filename, serial):
    """Render the constants block (Table 2-3) as CRLF-terminated triplet rows."""
    out = []
    for name, value, unit in _constant_rows(unix_start_time, raw_filename, serial):
        row = "{},{}".format(name, value)
        if unit:
            row += "," + unit
        row += ","            # trailing comma on every row (per the SoW example)
        out.append(row + _EOL)
    return "".join(out)


# Disable a writer after this many consecutive write failures (log the
# first failure and the disable) so a dead volume can't spam the debug
# log for the rest of an ascent.
MAX_WRITE_FAILURES = 5


class MetMessageWriter:
    """Base for the per-ascent ALM/TIM CSV writers (SoW 205192-11 2.2 / 2.3).

    Thread-safety mirrors RawMessageWriter: begin() and write_row() run on the
    MAVLink IO thread, and close() runs there too (from the gate's ascent-end
    hook) and -- for a mid-ascent disconnect -- on the main thread from the
    client's stop().  A lock guards the file handle so a close can never race an
    in-flight write.
    """

    PREFIX = ""        # filename prefix, e.g. "ALM" / "TIM"
    SUBDIR = ""        # Messages subfolder, e.g. "AltitudeLevel" / "TimeInterval"
    COLUMNS = ()       # list[Column] for the data block
    LOG_NAME = "met_message_writer"

    def __init__(self, log_dir=None, backup_dir=None):
        if log_dir is None:
            log_dir, backup_dir = self._default_dirs()
        self._dir = log_dir
        self._backup_dir = backup_dir
        self._fh = None
        self._path = None
        self._count = 0
        self._write_failures = 0
        self._first_origin = None   # this file's first data-row origin (Time-Since-Start baseline)
        self._lock = threading.Lock()
        self._log = get_logger(self.LOG_NAME)

    @property
    def path(self):
        return self._path

    # -- hooks a subclass may override -------------------------------------
    def _origin(self, record):
        """Per-row time origin for the 'Time Since Start' column.

        Default is the (bin-mean) sample time -- the ALM behavior.  TIM
        overrides this to the bin's start second so its times are integers.
        """
        return record.time

    # -- internals ---------------------------------------------------------
    def _default_dirs(self):
        """(primary, backup) ``Messages/<SUBDIR>`` directories.

        output_dirs() resolves the ``[usr access intended]`` base once, so the
        message folders always share a parent with the telemetry log on every
        platform (SoW 205195 #11); the backup is the built-in-storage mirror
        per #3 (None when there is nothing to mirror to).
        """
        return output_dirs("Messages", self.SUBDIR)

    def _header(self):
        """Two-line data header: comma-delimited names, then units."""
        return (",".join(c.name for c in self.COLUMNS) + _EOL
                + ",".join(c.unit for c in self.COLUMNS) + _EOL)

    # -- lifecycle ---------------------------------------------------------
    def begin(self, start_time, raw_path=None, serial="0", operator_string=""):
        """Open this ascent's file and write the constants + the data header.

        Called at the first ascending sample of an ascent (the gate's leading
        edge).  ``start_time`` is that sample's UNIX time -- it sets the
        filename's timestamp and the Unix Start Time constant.  ``raw_path`` is
        the open Raw file's path, for the Raw Data Filename constant (empty when
        there is no Raw file, e.g. during a replay).  ``serial`` is the drone
        serial number and ``operator_string`` the operator ID, both from the
        Remote ID settings: an empty ``serial`` falls back to "0", and an empty
        ``operator_string`` drops the filename's optional operator suffix.

        The constants and the two header lines are written and flushed here, so
        a well-formed file exists on disk before the first data row.
        """
        self.close()   # defensive: close anything left open by a prior ascent
        serial = serial or "0"   # empty/unset serial -> the "0" placeholder
        try:
            os.makedirs(self._dir, exist_ok=True)
            stamp = _utc(start_time).strftime("%Y%m%d_%H%M%S")
            suffix = "_{}".format(operator_string) if operator_string else ""
            base = "{}_{}_{}{}".format(self.PREFIX, serial, stamp, suffix)
            path = os.path.join(self._dir, base + ".csv")
            n = 1                                   # deconflict identical names
            while os.path.exists(path):
                path = os.path.join(self._dir, "{}_{}.csv".format(base, n))
                n += 1
            raw_filename = os.path.basename(raw_path) if raw_path else ""
            with self._lock:
                # newline="" => no newline translation; we emit explicit CRLF.
                self._fh = tee_open(self._dir, self._backup_dir,
                                    os.path.basename(path), "w",
                                    encoding="utf-8", newline="")
                self._path = path
                self._count = 0
                self._write_failures = 0
                self._first_origin = None
                self._fh.write(_constants_block(start_time, raw_filename, serial))
                self._fh.write(self._header())
                self._fh.flush()
            self._log.info("%s file opened: %s", self.PREFIX, path)
        except Exception:
            self._log.exception("Failed to open %s file", self.PREFIX)
            with self._lock:
                self._fh = None
                self._path = None

    def write_row(self, record):
        """Append one (binned) LevelRecord as a data row.

        The first row sets the Time-Since-Start origin (via ``_origin``), so its
        own Time Since Start reads 0.  A no-op when the file isn't open, so the
        caller never has to check.  The row is built from the same COLUMNS table
        that produced the header, so the two cannot drift.
        """
        with self._lock:
            if self._fh is None:
                return
            if self._first_origin is None:
                self._first_origin = self._origin(record)
            t0 = self._first_origin
            row = ",".join(c.fmt.format(c.get(record, t0)) for c in self.COLUMNS) + _EOL
            try:
                self._fh.write(row)
                self._fh.flush()
                self._count += 1
                self._write_failures = 0
            except Exception:
                self._write_failures += 1
                if self._write_failures == 1:
                    self._log.exception("Failed to write %s data row",
                                        self.PREFIX)
                if self._write_failures >= MAX_WRITE_FAILURES:
                    self._log.error("%s file disabled after %d "
                                    "consecutive write failures: %s",
                                    self.PREFIX, self._write_failures,
                                    self._path)
                    try:
                        self._fh.close()
                    except Exception:
                        pass
                    self._fh = None

    def close(self):
        """Flush and close the current file (if open)."""
        with self._lock:
            if self._fh is None:
                return
            try:
                self._fh.flush()
                self._fh.close()
                self._log.info("%s file closed: %s (%d data rows)",
                               self.PREFIX, self._path, self._count)
            except Exception:
                self._log.exception("Failed to close %s file", self.PREFIX)
            finally:
                self._fh = None
                self._path = None
                self._count = 0
                self._first_origin = None
