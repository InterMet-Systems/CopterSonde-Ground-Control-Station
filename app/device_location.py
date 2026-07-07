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

    _PROVIDER_TTL_S = 5.0  # cache Java provider queries this long

    def __init__(self, on_fix=None):
        self.on_fix = on_fix
        # Monotonic time of the most recent fix; None until the first one.
        # Fix freshness drives the Remote ID indicator (SoW 205195 #38).
        self.last_fix_time = None
        self._gps = None
        self._started = False
        # ── Diagnostics (readable from the Settings > Debug tab) ──
        self.start_result = "not started"
        self.last_fix = None          # (lat, lon) of most recent fix
        self.last_status = None       # most recent plyer status string
        self.last_status_time = None  # monotonic() of that status
        self._providers = None        # {name: enabled} or None if unknown
        self._providers_time = 0.0
        self._providers_error = None

    def start(self):
        """Begin location updates. Safe to call when unavailable: logs and
        returns, leaving the operator location "unknown"."""
        if self._started:
            return
        try:
            from plyer import gps
        except Exception as exc:
            self.start_result = f"plyer unavailable: {exc}"
            log.warning("plyer GPS unavailable — Remote ID operator "
                        "location will be 'unknown'")
            return
        try:
            gps.configure(on_location=self._on_location,
                          on_status=self._on_status)
            gps.start(minTime=1000, minDistance=0)
        except NotImplementedError:
            self.start_result = "no GPS implementation on this platform"
            log.info("No GPS implementation on this platform — Remote ID "
                     "operator location will be 'unknown'")
            return
        except Exception as exc:
            self.start_result = f"start failed: {exc!r}"
            log.exception("Failed to start device GPS")
            return
        self._gps = gps
        self._started = True
        self.start_result = "started"
        # Snapshot provider states right away so the app log records
        # whether the OS even offers a usable GNSS provider.
        states = self.provider_states()
        if states is not None:
            log.info("Location providers: %s", ", ".join(
                f"{n}({'enabled' if e else 'DISABLED'})"
                for n, e in sorted(states.items())))
        elif self._providers_error:
            log.warning("Provider query failed: %s", self._providers_error)
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
        self.last_fix = (lat, lon)
        cb = self.on_fix
        if cb is not None:
            cb(lat, lon)

    def _on_status(self, stype, status):
        self.last_status = f"{stype} {status}"
        self.last_status_time = time.monotonic()
        log.info("Device GPS status: %s %s", stype, status)

    # ── Diagnostics ─────────────────────────────────────────────────

    def provider_states(self):
        """Return ``{provider_name: enabled}`` from Android's
        LocationManager, or None where that can't be queried (non-Android,
        or the query failed — see ``_providers_error``).

        Cached for ``_PROVIDER_TTL_S`` so the 10 Hz Remote ID indicator
        isn't hammering JNI.
        """
        now = time.monotonic()
        if (self._providers is not None
                and now - self._providers_time < self._PROVIDER_TTL_S):
            return self._providers
        try:
            from jnius import autoclass
            from plyer.platforms.android import activity
            Context = autoclass('android.content.Context')
            lm = activity.getSystemService(Context.LOCATION_SERVICE)
            states = {}
            for prov in lm.getProviders(False).toArray():
                name = str(prov)
                states[name] = bool(lm.isProviderEnabled(name))
            self._providers = states
            self._providers_time = now
            self._providers_error = None
        except Exception as exc:
            self._providers_error = repr(exc)
            self._providers = None
        return self._providers

    def gps_provider_state(self):
        """'enabled' | 'disabled' | 'missing' | 'unknown' for the GNSS
        ('gps') provider.  'disabled' means Android Location is off or in
        battery-saving mode; 'missing' means the OS offers no GNSS
        provider at all."""
        states = self.provider_states()
        if states is None:
            return "unknown"
        if "gps" not in states:
            return "missing"
        return "enabled" if states["gps"] else "disabled"

    def _last_known_report(self):
        """One-line report of Android's cached last-known locations —
        evidence of whether providers have EVER produced a fix."""
        try:
            from jnius import autoclass
            from plyer.platforms.android import activity
            Context = autoclass('android.content.Context')
            System = autoclass('java.lang.System')
            lm = activity.getSystemService(Context.LOCATION_SERVICE)
            parts = []
            for prov in lm.getProviders(False).toArray():
                name = str(prov)
                loc = lm.getLastKnownLocation(name)
                if loc is None:
                    parts.append(f"{name}: none")
                else:
                    age_s = (System.currentTimeMillis() - loc.getTime()) / 1000
                    parts.append(f"{name}: {loc.getLatitude():.5f},"
                                 f"{loc.getLongitude():.5f} ({age_s:.0f}s old)")
            return "; ".join(parts) if parts else "no providers"
        except Exception as exc:
            return f"unavailable ({exc!r})"

    def diagnostics(self):
        """Multi-line human-readable state dump for the Debug tab."""
        lines = [f"Start: {self.start_result}"]
        states = self.provider_states()
        if states is not None:
            lines.append("Providers: " + ", ".join(
                f"{n}({'enabled' if e else 'DISABLED'})"
                for n, e in sorted(states.items())))
        else:
            lines.append(f"Providers: unknown ({self._providers_error})")
        if self.last_status is not None:
            age = time.monotonic() - self.last_status_time
            lines.append(f"Last status: {self.last_status} ({age:.0f}s ago)")
        else:
            lines.append("Last status: (no status callbacks yet)")
        if self.last_fix_time is not None:
            age = time.monotonic() - self.last_fix_time
            lat, lon = self.last_fix
            lines.append(f"Last fix: {lat:.5f},{lon:.5f} ({age:.0f}s ago)")
        else:
            lines.append("Last fix: NEVER")
        lines.append("Last known (OS cache): " + self._last_known_report())
        return "\n".join(lines)
