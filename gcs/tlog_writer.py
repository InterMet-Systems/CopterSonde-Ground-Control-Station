"""
Per-connection MAVLink telemetry log (.tlog) writer for CopterSonde GCS.

Implements requirement #30: dump all incoming telemetry to a binary file
exactly as received.  Writes the de facto standard MAVLink telemetry log
format used by Mission Planner, QGroundControl, MAVProxy, and pymavlink:

    [8-byte big-endian uint64: Unix time in microseconds][raw MAVLink frame]

repeated for every received message.  The frame bytes come straight from
``msg.get_msgbuf()`` — the exact wire bytes (v1/v2 framing, CRC, signature
if present), never re-encoded.  Because this matches what pymavlink itself
writes in ``mavfile.recv_msg()``, the resulting file can be opened by any
MAVLink tool, and replayed via ``mavutil.mavlink_connection(path)`` —
which is the natural foundation for requirement #33 (log replay).

Format notes:
  * The low two bits of the timestamp are masked off (& ~3) to match
    pymavlink, which reserves them as a link ID on the read side.
  * BAD_DATA frames are skipped, again matching pymavlink's own writer,
    so the file stays cleanly parseable.  Garbage frames are still
    visible in the human-readable MessageLogger output.

Lifecycle mirrors MessageLogger: open() on connect, log_message() per
received message (IO thread), close() on disconnect.
"""

import os
import struct
import threading
import time
from datetime import datetime

from gcs.logutil import get_logger
from gcs.storage_paths import resolve_base

log = get_logger("tlog_writer")

# Flush at least this often so a mid-flight crash loses at most ~1 s of
# telemetry.  (Line buffering isn't available in binary mode, so we can't
# reuse MessageLogger's buffering=1 trick.)
FLUSH_INTERVAL_S = 1.0


def _default_log_dir():
    return resolve_base("tlogs")


class TlogWriter:
    """Writes one .tlog file per connection.

    Thread-safety: open()/close() run on the main (UI) thread while
    log_message() runs on the MAVLink IO thread.  A lock guards the file
    handle so a write can never race with a close (same rationale as
    MessageLogger — stop()'s thread join can time out while the IO
    thread is still draining).
    """

    def __init__(self, log_dir=None):
        self._dir = log_dir or _default_log_dir()
        self._fh = None
        self._path = None
        self._count = 0
        self._last_flush = 0.0
        self._lock = threading.Lock()

    @property
    def path(self):
        return self._path

    def open(self):
        """Open a fresh datetime-named .tlog for a new connection."""
        # Defensive: close anything left open by a prior connection.
        self.close()
        try:
            os.makedirs(self._dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(self._dir, f"{stamp}.tlog")
            # Don't clobber if we reconnect within the same second.
            n = 1
            while os.path.exists(path):
                path = os.path.join(self._dir, f"{stamp}_{n}.tlog")
                n += 1
            with self._lock:
                self._fh = open(path, "wb")
                self._path = path
                self._count = 0
                self._last_flush = time.monotonic()
            log.info("Telemetry log opened: %s", path)
        except Exception:
            log.exception("Failed to open telemetry log")
            with self._lock:
                self._fh = None
                self._path = None

    def log_message(self, msg):
        """Append one received message: 8-byte timestamp + raw frame bytes."""
        if msg.get_type() == "BAD_DATA":
            return  # matches pymavlink's writer; see module docstring
        try:
            buf = msg.get_msgbuf()
        except Exception:
            return
        if not buf:
            return
        # Identical encoding to pymavlink's mavfile.recv_msg():
        # wall-clock microseconds, low two bits masked (reserved as a
        # link ID by tlog readers).
        usec = int(time.time() * 1.0e6) & ~3
        record = struct.pack(">Q", usec) + bytes(buf)

        with self._lock:
            if self._fh is None:
                return
            try:
                self._fh.write(record)
                self._count += 1
                now = time.monotonic()
                if now - self._last_flush >= FLUSH_INTERVAL_S:
                    self._fh.flush()
                    self._last_flush = now
            except Exception:
                log.exception("Failed to write telemetry log record")

    def close(self):
        """Flush and close the current file (if open).

        No EOF marker — the binary format has no room for one, and
        readers simply stop at end-of-file.
        """
        with self._lock:
            if self._fh is None:
                return
            try:
                self._fh.flush()
                self._fh.close()
                log.info("Telemetry log closed: %s (%d messages)",
                         self._path, self._count)
            except Exception:
                log.exception("Failed to close telemetry log")
            finally:
                self._fh = None
                self._path = None
                self._count = 0