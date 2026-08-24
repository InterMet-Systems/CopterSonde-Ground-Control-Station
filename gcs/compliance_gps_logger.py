"""TEMPORARY compliance-only GPS position logger (SoW 205195 #51).

Logs the GPS position (latitude, longitude, altitude) of both the
controller and the drone to a human-readable CSV, at least once every
4 seconds, for one-time regulatory compliance testing.

REMOVE BEFORE PRODUCTION.  This module is deliberately self-contained:
delete this file, the Settings > Testing toggle in app.kv, and the
"Compliance GPS logger (SoW #51)" blocks in app/main.py, and the feature
is gone.  Nothing else imports it.

Design notes:
  * The SoW requires logging "while not armed"; per the presiding
    manager, logging simply runs whenever connected — arming neither
    starts, stops, nor gates it.  That is a superset of the requirement.
  * Rows are written by a 2-second Kivy Clock tick on the main thread
    (see app/main.py), comfortably inside the 4-second requirement even
    with scheduling jitter.  open()/close() also run on the main thread,
    so unlike the telemetry writers this class needs no lock.
  * Format: one '#' comment header block, then a CSV header row, then
    data rows — human readable, and trivially parseable (skip lines
    starting with '#').
  * Unknown values are written as empty fields, never as 0.0 — a blank
    is honest, a fabricated (0, 0) fix is not.  fix_type is always
    written so a parser can judge drone-fix validity itself.
"""

import os
from datetime import datetime

from gcs.logutil import get_logger
from gcs.storage_paths import output_dirs, tee_open

log = get_logger("compliance_gps")

# Disable after this many consecutive write failures (log the first
# failure and the disable) so a dead volume can't spam the debug log.
MAX_WRITE_FAILURES = 5

_HEADER = (
    "# CopterSonde GCS compliance GPS position log (SoW 205195 #51)\n"
    "# TEMPORARY regulatory-testing output - not a user-facing product "
    "feature.\n"
    "# One row every ~2 s while connected. Empty field = value unknown "
    "(no fix / no data yet).\n"
    "# ctrl_* = controller (GCS device location services), "
    "drone_* = vehicle telemetry.\n"
    "# Altitudes in meters: ctrl_alt_m as supplied by the OS (GPS "
    "ellipsoid on Android), drone_alt_amsl_m is AMSL.\n"
    "unix_time,local_time,ctrl_lat,ctrl_lon,ctrl_alt_m,"
    "drone_lat,drone_lon,drone_alt_amsl_m,drone_fix_type\n"
)


def _default_dirs():
    # Its own folder under [usr access intended] so the compliance
    # artifacts are easy to collect - and the whole folder easy to
    # delete - without touching the user-facing Messages tree (#11).
    return output_dirs("ComplianceLog")


class ComplianceGpsLogger:
    """Writes one CSV file per logging session (open() .. close())."""

    def __init__(self, log_dir=None, backup_dir=None):
        if log_dir is None:
            log_dir, backup_dir = _default_dirs()
        self._dir = log_dir
        self._backup_dir = backup_dir
        self._fh = None
        self._path = None
        self._write_failures = 0

    @property
    def active(self):
        return self._fh is not None

    @property
    def path(self):
        return self._path

    def open(self):
        """Open a fresh datetime-named CSV. Failure is non-fatal: the
        logger just stays inactive (matching MessageLogger)."""
        self.close()  # defensive: a prior session left open
        try:
            os.makedirs(self._dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            name = f"compliance_gps_{stamp}.csv"
            # Don't clobber on reconnect within the same second.
            n = 1
            while os.path.exists(os.path.join(self._dir, name)):
                name = f"compliance_gps_{stamp}_{n}.csv"
                n += 1
            # Line-buffered so rows survive an abrupt power-off and the
            # file can be `tail -f`'d live during the test.
            self._fh = tee_open(self._dir, self._backup_dir, name, "w",
                                buffering=1)
            self._path = os.path.join(self._dir, name)
            self._write_failures = 0
            self._fh.write(_HEADER)
            log.info("Compliance GPS log opened: %s", self._path)
        except Exception:
            log.exception("Failed to open compliance GPS log")
            self._fh = None
            self._path = None

    def log_row(self, ctrl_fix, ctrl_alt, drone_lat, drone_lon,
                drone_alt_amsl, drone_fix_type):
        """Write one position row.

        ctrl_fix is (lat, lon) or None; ctrl_alt is meters or None.
        Drone values are written only when drone_fix_type >= 2 (2D fix) -
        vehicle-state lat/lon default to 0.0 before any telemetry, and a
        fabricated (0, 0) row would poison the compliance record.
        """
        if self._fh is None:
            return
        now = datetime.now()
        if ctrl_fix is not None:
            c_lat, c_lon = f"{ctrl_fix[0]:.7f}", f"{ctrl_fix[1]:.7f}"
        else:
            c_lat = c_lon = ""
        c_alt = f"{ctrl_alt:.1f}" if ctrl_alt is not None else ""
        if drone_fix_type >= 2:
            d_lat, d_lon = f"{drone_lat:.7f}", f"{drone_lon:.7f}"
            d_alt = f"{drone_alt_amsl:.1f}"
        else:
            d_lat = d_lon = d_alt = ""
        row = (f"{now.timestamp():.3f},"
               f"{now.strftime('%Y-%m-%d %H:%M:%S')},"
               f"{c_lat},{c_lon},{c_alt},"
               f"{d_lat},{d_lon},{d_alt},{drone_fix_type}\n")
        try:
            self._fh.write(row)
            self._write_failures = 0
        except Exception:
            self._write_failures += 1
            if self._write_failures == 1:
                log.exception("Compliance GPS log write failed")
            if self._write_failures >= MAX_WRITE_FAILURES:
                log.error("Compliance GPS log disabled after %d consecutive "
                          "write failures", self._write_failures)
                self.close()

    def close(self):
        if self._fh is None:
            return
        try:
            self._fh.close()
        except Exception:
            log.exception("Error closing compliance GPS log")
        log.info("Compliance GPS log closed: %s", self._path)
        self._fh = None
