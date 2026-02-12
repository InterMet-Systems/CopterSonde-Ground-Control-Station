"""
Logging utility for CopterSonde GCS.

Provides a file-based logger useful for debugging on Android where
stdout/stderr may not be easily accessible.
"""

import logging
import os
import sys
from datetime import datetime

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LOG_DIR = None  # Set at runtime; defaults chosen per-platform below
LOG_LEVEL = logging.DEBUG
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_initialised = False


def _default_log_dir():
    """Return a sensible default log directory for the current platform."""
    # Prefer user-visible internal storage on Android (requires runtime permission)
    try:
        from android.storage import primary_external_storage_path  # type: ignore
        return os.path.join(primary_external_storage_path(),
                            "CopterSondeGCS", "logs")
    except ImportError:
        pass
    # Fallback to app-private storage on Android (always writable, not visible)
    try:
        from android.storage import app_storage_path  # type: ignore
        return os.path.join(app_storage_path(), "logs")
    except ImportError:
        pass
    # Windows / desktop Linux
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")


def setup_logging(log_dir=None, level=None):
    """
    Initialise file + console logging.  Safe to call multiple times;
    only the first call configures handlers.

    Console logging is set up first so the app can start even if file
    logging fails (e.g. missing storage permissions on Android).
    """
    global _initialised, LOG_DIR
    if _initialised:
        return
    _initialised = True

    root = logging.getLogger()
    root.setLevel(level or LOG_LEVEL)

    # Console handler — always works, set up first
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    root.addHandler(ch)

    # File handler — best-effort; may fail on Android without storage permission
    LOG_DIR = log_dir or _default_log_dir()
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = os.path.join(LOG_DIR, f"gcs_{timestamp}.log")
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
        root.addHandler(fh)
        logging.info("Logging initialised -> %s", log_file)
    except Exception:
        logging.warning("File logging unavailable — console only")


def get_logger(name: str) -> logging.Logger:
    """Return a named logger.  Call setup_logging() first."""
    return logging.getLogger(name)
