"""
Per-connection MAVLink message logger for CopterSonde GCS.

Each vehicle connection gets its own file:
  - open()         -> create a fresh datetime-stamped file
  - log_message()  -> record one received MAVLink message
  - close()        -> write an EOF marker and close the file

For now log_message() writes a single timestamped line per message;
payload serialization will be layered on later without touching the
rest of the GCS — only this module changes.
"""

import os
import threading
from datetime import datetime

from gcs.logutil import get_logger
from gcs.storage_paths import output_dirs, tee_open

log = get_logger("msg_logger")

# Disable the logger after this many consecutive write failures (log the
# first failure and the disable) so a dead volume can't spam the debug
# log at telemetry rates.
MAX_WRITE_FAILURES = 5


def _default_dirs():
    # SoW 205195 #10: this per-connection MAVLink dump is app-level diagnostic
    # output (no written spec, not user-facing), so it lives in the
    # Messages/Debug folder (#11).  output_dirs() resolves the base once, so
    # the whole Messages tree shares one parent with TelemetryLog on every
    # platform, and gives the built-in-storage mirror directory per #3.
    return output_dirs("Messages", "Debug")


def _timestamp():
    """Millisecond-resolution timestamp string."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class MessageLogger:
    """Writes one log file per connection.

    Thread-safety: open()/close() run on the main (UI) thread while
    log_message() runs on the MAVLink IO thread. A lock guards the file
    handle so a write can never race with a close (e.g. if stop()'s
    thread join times out and the IO thread is still draining).
    """

    def __init__(self, log_dir=None, backup_dir=None):
        if log_dir is None:
            log_dir, backup_dir = _default_dirs()
        self._dir = log_dir
        self._backup_dir = backup_dir
        self._fh = None
        self._path = None
        self._count = 0
        self._write_failures = 0
        self._lock = threading.Lock()

    @property
    def path(self):
        return self._path

    def open(self):
        """Open a fresh datetime-named file for a new connection."""
        # Defensive: close anything left open by a prior connection.
        self.close()
        try:
            os.makedirs(self._dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(self._dir, f"mavlink_{stamp}.log")
            # Don't clobber if we reconnect within the same second.
            n = 1
            while os.path.exists(path):
                path = os.path.join(self._dir, f"mavlink_{stamp}_{n}.log")
                n += 1
            with self._lock:
                # Line-buffered so you can `tail -f` it live during testing.
                self._fh = tee_open(self._dir, self._backup_dir,
                                    os.path.basename(path), "w", buffering=1)
                self._path = path
                self._count = 0
                self._write_failures = 0
            log.info("MAVLink message log opened: %s", path)
        except Exception:
            log.exception("Failed to open MAVLink message log")
            with self._lock:
                self._fh = None
                self._path = None

    def _format_message(self, msg):
        """Render one MAVLink message as a single human-readable line."""
        ts = _timestamp()
        name = msg.get_type()

        # BAD_DATA frames (malformed/partial packets on a UDP wire) don't have
        # a normal header or fields — log them compactly instead of crashing.
        if name == "BAD_DATA":
            return f"{ts} BAD_DATA ({len(msg.data)} bytes)\n"

        msg_id = msg.get_msgId()
        src = f"{msg.get_srcSystem()}/{msg.get_srcComponent()}"
        # to_dict() -> ordered {field: value} plus a 'mavpackettype' name key,
        # which we drop since the name is already in the line.
        fields = msg.to_dict()
        fields.pop("mavpackettype", None)
        body = ", ".join(f"{k}={v}" for k, v in fields.items())
        return f"{ts} {name} (#{msg_id}) src={src} {{{body}}}\n"

    def log_message(self, msg=None):
        """Record one received MAVLink message as a human-readable line."""
        # LOG_DATA is ~11,600 90-byte chunks per 1 MB drone-log download;
        # dumping each as text would add several MB of noise per fetch.
        if msg.get_type() == "LOG_DATA":
            return
        with self._lock:
            if self._fh is None:
                return
            try:
                self._fh.write(self._format_message(msg))
                self._count += 1
                self._write_failures = 0
            except Exception:
                self._write_failures += 1
                if self._write_failures == 1:
                    log.exception("Failed to write to MAVLink message log")
                if self._write_failures >= MAX_WRITE_FAILURES:
                    log.error("MAVLink message log disabled after %d "
                              "consecutive write failures: %s",
                              self._write_failures, self._path)
                    try:
                        self._fh.close()
                    except Exception:
                        pass
                    self._fh = None

    def close(self):
        """Write an EOF marker and close the current file (if open)."""
        with self._lock:
            if self._fh is None:
                return
            try:
                self._fh.write(f"{_timestamp()} EOF ({self._count} messages)\n")
                self._fh.flush()
                self._fh.close()
                log.info("MAVLink message log closed: %s (%d messages)",
                         self._path, self._count)
            except Exception:
                log.exception("Failed to close MAVLink message log")
            finally:
                self._fh = None
                self._path = None
                self._count = 0