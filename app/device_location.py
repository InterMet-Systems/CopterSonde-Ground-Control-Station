"""Device (GCS) location source for Remote ID.

Wraps plyer's GPS facade on Android; there is no location source wired up
on other platforms, so OPEN_DRONE_ID_SYSTEM reports "unknown" there.

start()/stop() must be called from the main thread (plyer requirement).
The on_fix callback fires on a platform (Java) thread — keep it limited
to atomic attribute assignments.
"""

import time

from gcs.logutil import get_logger

log = get_logger("device_location")


class DeviceLocation:
    """Feeds device GPS fixes to a callback.

    on_fix(lat, lon) is called with decimal degrees on every update.
    """

    def __init__(self, on_fix=None):
        self.on_fix = on_fix
        # Monotonic time of the most recent fix; None until the first one.
        # Fix freshness drives the Remote ID indicator (SoW 205195 #38).
        self.last_fix_time = None
        self._gps = None
        self._started = False

    def start(self):
        """Begin location updates. Safe to call when unavailable: logs and
        returns, leaving the operator location "unknown"."""
        if self._started:
            return
        try:
            from plyer import gps
        except Exception:
            log.warning("plyer GPS unavailable — Remote ID operator "
                        "location will be 'unknown'")
            return
        try:
            gps.configure(on_location=self._on_location,
                          on_status=self._on_status)
            gps.start(minTime=1000, minDistance=0)
        except NotImplementedError:
            log.info("No GPS implementation on this platform — Remote ID "
                     "operator location will be 'unknown'")
            return
        except Exception:
            log.exception("Failed to start device GPS")
            return
        self._gps = gps
        self._started = True
        log.info("Device GPS started")

    def has_recent_fix(self, max_age_s=10.0):
        """True if a fix arrived within the last ``max_age_s`` seconds."""
        t = self.last_fix_time  # single read — written by a platform thread
        return t is not None and (time.monotonic() - t) <= max_age_s

    def stop(self):
        if not self._started:
            return
        try:
            self._gps.stop()
        except Exception:
            log.exception("Failed to stop device GPS")
        self._started = False

    # -- plyer callbacks (arrive on a platform thread) ------------------

    def _on_location(self, **kwargs):
        lat = kwargs.get("lat")
        lon = kwargs.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return
        self.last_fix_time = time.monotonic()
        cb = self.on_fix
        if cb is not None:
            cb(lat, lon)

    def _on_status(self, stype, status):
        log.info("Device GPS status: %s %s", stype, status)
