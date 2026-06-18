"""
Per-connection "Raw" data-file writer for CopterSonde GCS.

Writes the RAW data file into a ``Raw`` folder inside a ``Messages`` folder
that always sits next to the telemetry log's ``TelemetryLog`` folder, with
the same platform-dependent placement (built-in storage on desktop, the
micro SD card on Android — see gcs.storage_paths and gcs.tlog_writer).

File format (Raw File SoW, Table 1-3):
  * UTF-8 encoding; DOS (CRLF) line endings on every line.
  * Two header lines: row 1 = column names, row 2 = column units.
  * Comma-delimited columns, in the order given by RAW_COLUMNS.
  * Data rows follow the header, one per record — wired up in a later phase.
  * Filename ``RAW_MM-DD-YYYY_HH-MM-SS.txt`` where the date/time is the UTC
    system time at file creation.  A fresh file is created for each viable
    vehicle connection (open() runs when the first message arrives).

Lifecycle mirrors TlogWriter: open() on first-message arrival (MAVLink IO
thread), log_message() per received message (IO thread), close() on
disconnect (main/UI thread).
"""

import os
import threading
from datetime import datetime, timezone

from gcs.logutil import get_logger
from gcs.storage_paths import resolve_base

log = get_logger("raw_msg_writer")

# Column layout for the Raw file, in order (SoW Table 1-3): (name, unit).
# The data-row values that fill these columns are wired up in a later phase;
# for now only the two header lines (names, then units) are written.
RAW_COLUMNS = [
    ("Time",           "s"),
    ("ASL",            "m"),
    ("Pressure",       "mB"),
    ("Air Temp A",     "C"),
    ("Air Temp B",     "C"),
    ("Air Temp C",     "C"),
    ("Rel Hum A",      "%"),
    ("Rel Hum B",      "%"),
    ("Rel Hum C",      "%"),
    ("Latitude",       "deg"),
    ("Longitude",      "deg"),
    ("Roll",           "deg"),
    ("Roll Rate",      "deg/s"),
    ("Pitch",          "deg"),
    ("Pitch Rate",     "deg/s"),
    ("Yaw",            "deg"),
    ("Yaw Rate",       "deg/s"),
    ("Velocity North", "m/s"),
    ("Velocity East",  "m/s"),
    ("Velocity Down",  "m/s"),
    ("Custom Mode",    "N/A"),
]

# Filename: RAW_MM-DD-YYYY_HH-MM-SS.txt (UTC system time at creation).
_FILENAME_TIME_FORMAT = "RAW_%m-%d-%Y_%H-%M-%S"

# DOS line ending mandated by the SoW.  The file is opened with newline=""
# so this is written verbatim on every platform — including the Android /
# HereLink target, where a default text-mode write would emit a bare LF.
_EOL = "\r\n"

# Two-line header: comma-delimited column names, then comma-delimited units.
_HEADER = (
    ",".join(name for name, _unit in RAW_COLUMNS) + _EOL
    + ",".join(unit for _name, unit in RAW_COLUMNS) + _EOL
)


def _default_log_dir():
    """Return the ``Messages/Raw`` directory, always a sibling of TelemetryLog.

    We resolve the telemetry log's directory exactly the way TlogWriter does
    (``resolve_base("TelemetryLog", prefer_removable=True)``) and then place
    ``Messages/Raw`` next to it.  Deriving from the resolved TelemetryLog path
    — rather than calling ``resolve_base("Messages/Raw")`` independently —
    guarantees the two folders share the same parent on every platform, even
    in the corner cases where independent resolution could diverge (e.g. which
    volume wins on Android when an SD card is present).
    """
    telemetry_dir = resolve_base("TelemetryLog", prefer_removable=True)
    base = os.path.dirname(telemetry_dir)
    return os.path.join(base, "Messages", "Raw")


class RawMessageWriter:
    """Writes one Raw data file per connection.

    Thread-safety mirrors TlogWriter/MessageLogger: open() and log_message()
    run on the MAVLink IO thread (the file is opened lazily when the first
    message arrives), while close() runs on the main (UI) thread from stop().
    A lock guards the file handle so a write can never race with a close.
    """

    def __init__(self, log_dir=None):
        self._dir = log_dir or _default_log_dir()
        self._fh = None
        self._path = None
        self._count = 0
        self._lock = threading.Lock()

    @property
    def path(self):
        return self._path

    def open(self):
        """Create a fresh ``RAW_<UTC timestamp>.txt`` and write the header.

        Called when the first MAVLink message of a connection arrives (from
        the client's ``_open_telemetry_log`` hook), so the file appears at the
        same instant as the .tlog.  The two header lines are written and
        flushed here so a well-formed file exists on disk immediately, even
        before any data row is written.
        """
        # Defensive: close anything left open by a prior connection.
        self.close()
        try:
            os.makedirs(self._dir, exist_ok=True)
            # UTC system time at creation, per the SoW.
            stamp = datetime.now(timezone.utc).strftime(_FILENAME_TIME_FORMAT)
            path = os.path.join(self._dir, f"{stamp}.txt")
            # Don't clobber if two connections land in the same second
            # (mirrors TlogWriter / MessageLogger).
            n = 1
            while os.path.exists(path):
                path = os.path.join(self._dir, f"{stamp}_{n}.txt")
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

    def log_message(self, msg=None):
        """Record one received MAVLink message as a data row.

        Still a no-op at this stage: the filename and two-line header are in
        place, but appending per-record data rows (formatted to the SoW's
        per-column precision and CRLF-terminated) is the next phase and gets
        filled in here — guarded by the same ``self._lock`` + ``self._fh is
        None`` check, without touching the rest of the GCS.
        """
        return

    def close(self):
        """Flush and close the current file (if open)."""
        with self._lock:
            if self._fh is None:
                return
            try:
                self._fh.flush()
                self._fh.close()
                log.info("Raw data file closed: %s", self._path)
            except Exception:
                log.exception("Failed to close raw data file")
            finally:
                self._fh = None
                self._path = None
                self._count = 0
