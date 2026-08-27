"""Device (GCS) location source for Remote ID.

Talks to Android's LocationManager directly through pyjnius — no plyer.
pyjnius ships with every python-for-android build (Kivy requires it), so
there is no separately-packaged dependency to silently fall out of the
APK, which is exactly what happened to plyer in the field on 2026-07-09.

There is no location source wired up on other platforms, so
OPEN_DRONE_ID_SYSTEM reports "unknown" there and the Remote ID indicator
shows its yellow desktop state.

start()/stop() must be called from the main thread.  Location callbacks
arrive on the Android main looper (a platform thread, not Kivy's) — the
on_fix callback must stay limited to atomic attribute assignments.

Altitude is captured for display only (Flight-screen controller tiles,
SoW 205195 #46).  The Remote ID on_fix path carries lat/lon only —
operator_altitude_geo stays "unknown" (-1000) per SoW #37.
"""

import time

from gcs.logutil import get_logger

log = get_logger("device_location")

# Strong module-level cache of the generated listener class.  The
# PythonJavaClass subclass can only be defined once jnius is importable,
# so it is built lazily on Android and never on the desktop.
_listener_cls = None


def _android_activity():
    """The running Android Activity (the Context for system services).

    This is the same object plyer's ``platforms.android.activity``
    resolved to; going through PythonActivity directly removes the plyer
    import.  Raises on non-Android platforms (jnius absent).
    """
    from jnius import autoclass
    return autoclass("org.kivy.android.PythonActivity").mActivity


def _location_manager():
    from jnius import autoclass
    Context = autoclass("android.content.Context")
    return _android_activity().getSystemService(Context.LOCATION_SERVICE)


def _get_listener_cls():
    """Build (once) the Java LocationListener implemented in Python.

    Defined inside a function because PythonJavaClass and the
    @java_method decorators need jnius at class-definition time, and
    this module must stay importable on the desktop.
    """
    global _listener_cls
    if _listener_cls is not None:
        return _listener_cls

    from jnius import PythonJavaClass, java_method

    class _Listener(PythonJavaClass):
        __javainterfaces__ = ["android/location/LocationListener"]
        __javacontext__ = "app"

        def __init__(self, owner):
            super().__init__()
            self._owner = owner

        @java_method("(Landroid/location/Location;)V")
        def onLocationChanged(self, location):
            # Altitude is optional on an Android Location (network fixes
            # usually lack it); pass None rather than the 0.0 that
            # getAltitude() returns when hasAltitude() is false.
            self._owner._on_location(
                lat=location.getLatitude(),
                lon=location.getLongitude(),
                accuracy=location.getAccuracy(),
                altitude=(location.getAltitude()
                          if location.hasAltitude() else None))

        @java_method("(Ljava/lang/String;)V")
        def onProviderEnabled(self, provider):
            self._owner._on_status("provider-enabled", str(provider))

        @java_method("(Ljava/lang/String;)V")
        def onProviderDisabled(self, provider):
            self._owner._on_status("provider-disabled", str(provider))

        @java_method("(Ljava/lang/String;ILandroid/os/Bundle;)V")
        def onStatusChanged(self, provider, status, extras):
            # Deprecated after API 29 but still delivered on the
            # Herelink's API 25; must be implemented or its invocation
            # raises AbstractMethodError inside the framework.
            self._owner._on_status("provider-status", f"{provider} {status}")

    _listener_cls = _Listener
    return _Listener


class DeviceLocation:
    """Feeds device GPS fixes to a callback.

    on_fix(lat, lon) is called with decimal degrees on every update.
    """

    _PROVIDER_TTL_S = 5.0  # cache Java provider queries this long
    _MIN_TIME_MS = 1000    # requestLocationUpdates minTime
    _MIN_DISTANCE_M = 0.0  # requestLocationUpdates minDistance

    def __init__(self, on_fix=None):
        self.on_fix = on_fix
        # Monotonic time of the most recent fix; None until the first one.
        # Fix freshness drives the Remote ID indicator (SoW 205195 #38).
        self.last_fix_time = None
        self._started = False
        self._listener = None   # strong ref — GC'd PythonJavaClass = dead callbacks
        self._lm = None
        # ── Diagnostics (readable from the Settings > Debug tab) ──
        self.start_result = "not started"
        self.last_fix = None          # (lat, lon) of most recent fix
        self.last_alt = None          # altitude [m, WGS84 ellipsoid] of that
                                      # fix, or None if the provider had none
        self.last_accuracy = None     # meters, from the same fix
        self.last_status = None       # most recent status string
        self.last_status_time = None  # monotonic() of that status
        self.subscribed = []          # providers we requested updates from
        self._providers = None        # {name: enabled} or None if unknown
        self._providers_time = 0.0
        self._providers_error = None

    def start(self):
        """Begin location updates.  Safe to call when unavailable: logs and
        returns, leaving the operator location "unknown"."""
        if self._started:
            return
        try:
            from jnius import autoclass  # noqa: F401 — probe for Android
        except Exception as exc:
            self.start_result = f"jnius unavailable (not Android?): {exc}"
            log.info("No location source on this platform — Remote ID "
                     "operator location will be 'unknown'")
            return
        try:
            from jnius import autoclass
            Looper = autoclass("android.os.Looper")
            self._lm = _location_manager()
            self._listener = _get_listener_cls()(self)
            providers = [str(p) for p in self._lm.getProviders(False).toArray()]
            self.subscribed = []
            errors = []
            for name in providers:
                try:
                    # Subscribe even to currently-disabled providers: if the
                    # operator enables Location mid-session, updates begin
                    # without an app restart (onProviderEnabled fires too).
                    self._lm.requestLocationUpdates(
                        name, self._MIN_TIME_MS, self._MIN_DISTANCE_M,
                        self._listener, Looper.getMainLooper())
                    self.subscribed.append(name)
                except Exception as exc:   # SecurityException, etc.
                    errors.append(f"{name}: {exc!r}")
            if not self.subscribed:
                detail = "; ".join(errors) if errors else "no providers offered"
                self.start_result = f"no provider subscriptions ({detail})"
                log.warning("Device GPS: could not subscribe to any "
                            "location provider: %s", detail)
                return
        except Exception as exc:
            self.start_result = f"start failed: {exc!r}"
            log.exception("Failed to start device GPS")
            return
        self._started = True
        self.start_result = "started (subscribed: {})".format(
            ", ".join(self.subscribed))
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
            self._lm.removeUpdates(self._listener)
        except Exception:
            log.exception("Failed to stop device GPS")
        self._started = False

    # ── Callbacks (arrive on the Android main looper thread) ──────────

    def _on_location(self, lat=None, lon=None, accuracy=None,
                     altitude=None, **_):
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return
        self.last_fix_time = time.monotonic()
        self.last_fix = (lat, lon)
        # Captured for display only; the on_fix path below is deliberately
        # lat/lon-only (see module docstring).
        self.last_alt = altitude if isinstance(altitude, (int, float)) else None
        self.last_accuracy = accuracy
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
            lm = self._lm if self._lm is not None else _location_manager()
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
            System = autoclass("java.lang.System")
            lm = self._lm if self._lm is not None else _location_manager()
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
            acc = (f", +/-{self.last_accuracy:.0f}m"
                   if self.last_accuracy is not None else "")
            alt = (f", alt {self.last_alt:.1f}m"
                   if self.last_alt is not None else ", alt n/a")
            lines.append(
                f"Last fix: {lat:.5f},{lon:.5f} ({age:.0f}s ago{acc}{alt})")
        else:
            lines.append("Last fix: NEVER")
        lines.append("Last known (OS cache): " + self._last_known_report())
        return "\n".join(lines)
