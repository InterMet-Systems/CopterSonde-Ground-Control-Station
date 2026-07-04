"""
Logging utility for CopterSonde GCS.

Provides a file-based logger useful for debugging on Android where
stdout/stderr may not be easily accessible.
"""

import logging
import os
import sys
from datetime import datetime
from collections import deque

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LOG_DIR = None  # Set at runtime; defaults chosen per-platform below
LOG_LEVEL = logging.DEBUG
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_initialised = False  # guard to ensure setup_logging() runs only once
_file_attached = False  # guard to ensure file handlers attach only once

_LOG_RING = deque(maxlen=500)  # recent formatted log lines, for in-app display

class _RingBufferHandler(logging.Handler):
    """Keeps the most recent formatted log lines in memory for on-device view."""
    def emit(self, record):
        try:
            _LOG_RING.append(self.format(record))
        except Exception:
            pass


def get_recent_logs():
    """Return recent log lines (oldest first) — for the in-app debug view."""
    return list(_LOG_RING)


def _default_log_dir():
    """Fallback log directory, used only if gcs.storage_paths can't resolve.

    The normal path is attach_file_handler(), which places the debug log in
    the unified ``[usr access intended]/Messages/Debug`` tree (SoW 205195
    #10/#11) alongside the MAVLink message dump.  This fallback keeps file
    logging alive if that resolution itself blows up.

    Android storage fallback chain:
      1. primary_external_storage_path — user-visible (e.g. /sdcard/),
         but requires WRITE_EXTERNAL_STORAGE permission at runtime.
      2. app_storage_path — always writable but hidden from the user
         (app-private internal storage).
      3. Desktop fallback — ../Messages/Debug relative to this file.
    """
    # 1st choice: user-visible external storage on Android
    try:
        from android.storage import primary_external_storage_path  # type: ignore
        return os.path.join(primary_external_storage_path(),
                            "CopterSondeGCS", "Messages", "Debug")
    except ImportError:
        pass
    # 2nd choice: app-private internal storage on Android (always writable)
    try:
        from android.storage import app_storage_path  # type: ignore
        return os.path.join(app_storage_path(), "Messages", "Debug")
    except ImportError:
        pass
    # 3rd choice: desktop (Windows / Linux) — project-relative Messages/Debug
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "Messages", "Debug")


def setup_logging(log_dir=None, level=None):
    """
    Initialise console + ring-buffer logging.  Safe to call multiple times;
    only the first call configures handlers.

    File logging: with an explicit ``log_dir`` it is attached here (legacy
    / scripting path).  With ``log_dir=None`` — the app's path — it is
    deferred to attach_file_handler(), which resolves the unified
    ``Messages/Debug`` location via gcs.storage_paths.  The deferral exists
    because storage_paths imports this module, so the logger must come up
    before storage resolution can run (or log its own attempts).
    """
    global _initialised
    if _initialised:
        return
    _initialised = True

    root = logging.getLogger()
    root.setLevel(level or LOG_LEVEL)

    # Console handler is set up FIRST so the app has working log output
    # even if file logging fails (e.g. missing storage permission on Android).
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)  # console only gets INFO+; DEBUG goes to file
    ch.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    root.addHandler(ch)

    rb = _RingBufferHandler()
    rb.setLevel(logging.DEBUG)  # capture everything; deque bounds memory
    rb.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    root.addHandler(rb)

    if log_dir is not None:
        _attach_file_handlers([log_dir])


def _attach_file_handlers(dirs):
    """Attach a DEBUG-level FileHandler in each directory of ``dirs``.

    All handlers share one timestamped filename.  ``dirs`` entries may be
    None (skipped).  Each attach is best-effort; the first successful
    directory becomes LOG_DIR.
    """
    global _file_attached, LOG_DIR
    _file_attached = True

    root = logging.getLogger()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"gcs_{timestamp}.log"
    attached = []
    for d in dirs:
        if not d:
            continue
        try:
            os.makedirs(d, exist_ok=True)
            fh = logging.FileHandler(os.path.join(d, filename),
                                     encoding="utf-8")
            fh.setLevel(logging.DEBUG)  # everything to file for post-flight analysis
            fh.setFormatter(logging.Formatter(LOG_FORMAT,
                                              datefmt=LOG_DATE_FORMAT))
            root.addHandler(fh)
            attached.append(d)
        except Exception:
            logging.warning("File logging unavailable at %s", d)

    if attached:
        LOG_DIR = attached[0]
        logging.info("Logging initialised -> %s",
                     os.path.join(attached[0], filename))
        for extra in attached[1:]:
            logging.info("Debug log mirrored -> %s", extra)
    else:
        logging.warning("File logging unavailable — console only")


def attach_file_handler():
    """Attach the debug-log file handler(s) at the unified location.

    Resolves ``[usr access intended]/Messages/Debug`` (SoW 205195 #10/#11)
    via gcs.storage_paths — the same tree the MAVLink message dump uses —
    and mirrors to the built-in-storage backup when the primary is the SD
    card (#3), by attaching one FileHandler per location.  storage_paths
    is imported lazily because it imports this module at load time.

    No-op if file handlers are already attached (e.g. setup_logging() was
    given an explicit log_dir).
    """
    if _file_attached:
        return
    try:
        from gcs.storage_paths import output_dirs
        primary, backup = output_dirs("Messages", "Debug")
    except Exception:
        logging.exception("Storage resolution failed; using legacy log dir")
        primary, backup = _default_log_dir(), None
    _attach_file_handlers([primary, backup])


def get_logger(name: str) -> logging.Logger:
    """Return a named logger.  Call setup_logging() first."""
    return logging.getLogger(name)
