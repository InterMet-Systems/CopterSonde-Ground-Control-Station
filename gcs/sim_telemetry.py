"""
Simulated telemetry generator for demo / testing without a real vehicle.

Produces a moving GPS track, changing attitude/heading/altitude,
temperature and RH curves, wind changes, and a few ADS-B tracks.
Populates a ``VehicleState`` and emits ``DATA_UPDATED`` events via the
``EventBus`` at 10 Hz.
"""

import math
import random
import threading
import time

from gcs.logutil import get_logger
from gcs.vehicle_state import VehicleState, ADSBTarget, StatusMessage

log = get_logger("sim_telemetry")

# Base position – Norman, OK (OU/CASS weather station site)
BASE_LAT = 35.2226
BASE_LON = -97.4395
BASE_ALT = 357.0  # AMSL meters

# Wind estimation coefficients — must match mavlink_client.py so the sim
# produces realistic wind speeds from the same pitch-based formula.
WS_A = 37.1
WS_B = 3.8


class SimTelemetry:
    """Background thread that generates synthetic telemetry."""

    def __init__(self, state: VehicleState = None, event_bus=None):
        self.state = state or VehicleState()
        self.event_bus = event_bus
        self._thread = None
        self._stop = threading.Event()
        self.running = False

        # Wind estimation coefficients (mutable; updated from Settings)
        self.ws_a = WS_A
        self.ws_b = WS_B

        # Sim clock — _t0 is monotonic time at start(); _boot_time mimics
        # the autopilot's time_boot_ms field.
        self._t0 = 0.0
        self._boot_time = 0.0

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._t0 = time.monotonic()
        self._boot_time = 0.0
        self.state.reset()
        self._seed_state()
        self._thread = threading.Thread(
            target=self._loop, name="sim-telemetry", daemon=True
        )
        self._thread.start()
        self.running = True
        log.info("Sim telemetry started")

        if self.event_bus:
            from gcs.event_bus import EventType
            self.event_bus.emit(EventType.CONNECTION_CHANGED,
                                {"connected": True, "demo": True})

    def stop(self):
        if not self.running:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        self.running = False
        log.info("Sim telemetry stopped")

        if self.event_bus:
            from gcs.event_bus import EventType
            self.event_bus.emit(EventType.CONNECTION_CHANGED,
                                {"connected": False})

    def _seed_state(self):
        """Set initial plausible values (pre-arm, on the ground)."""
        s = self.state
        s.lat = BASE_LAT
        s.lon = BASE_LON
        s.alt_amsl = BASE_ALT
        s.alt_rel = 0.0
        s.fix_type = 3       # 3D GPS fix
        s.satellites = 14
        s.hdop = 0.95
        s.voltage = 25.2
        s.current = 0
        s.battery_pct = 98
        s.rssi_percent = 85
        s.armed = False
        s.flight_mode = "STABILIZE"
        s.last_heartbeat = time.monotonic()

        # Seed a few synthetic ADS-B targets nearby for map display testing
        for i in range(3):
            icao = 0xABCD00 + i
            s.adsb_targets[icao] = ADSBTarget(
                icao=icao,
                callsign=f"SIM{i+1:03d}",
                lat=BASE_LAT + random.uniform(-0.03, 0.03),
                lon=BASE_LON + random.uniform(-0.03, 0.03),
                alt_m=BASE_ALT + random.uniform(500, 3000),
                heading=random.uniform(0, 360),
                speed_ms=random.uniform(50, 120),
                last_seen=time.monotonic(),
            )

    def _loop(self):
        """Main sim loop at ~10 Hz."""
        while not self._stop.is_set():
            dt = time.monotonic() - self._t0
            self._boot_time = dt
            self._update(dt)

            if self.event_bus:
                from gcs.event_bus import EventType
                self.event_bus.emit(EventType.DATA_UPDATED,
                                    self.state.snapshot())
            time.sleep(0.1)

    def _update(self, t):
        """Update all simulated telemetry for time t (seconds since start).

        Simulated flight phases:
          0-10 s       : Pre-arm idle on the ground (STABILIZE mode)
          10-260 s     : Ascent — climb at 4 m/s to 1000 m AGL (GUIDED mode)
          260-510 s    : Descent — descend at 4 m/s back to 0 m AGL
          510+ s       : Landed idle on the ground (STABILIZE mode)

        Atmospheric model (realistic mid-latitude summer profile):
          - Temperature: surface 25 C, standard lapse rate -6.5 C/km with
            a mild inversion layer at 400-500 m and small turbulent noise.
          - Humidity: surface 65%, increases with altitude through the
            boundary layer, peaks near 85% around 600 m (cloud base),
            then drops above.
          - Wind: increases logarithmically from surface, with a low-level
            jet feature around 700 m and directional veering with height.
        """
        s = self.state
        s.last_heartbeat = time.monotonic()
        s.time_since_boot = t

        climb_rate = 4.0        # m/s
        max_alt = 1000.0        # m AGL
        ascent_dur = max_alt / climb_rate   # 250 s
        descent_dur = ascent_dur            # 250 s
        prearm_dur = 10.0

        # --- Flight phase logic ---
        if t <= prearm_dur:
            # Pre-arm idle on the ground
            s.set_armed(False)
            s.flight_mode = "STABILIZE"
            s.alt_rel = 0.0
            s.vz = 0
        elif t <= prearm_dur + ascent_dur:
            # Ascent
            s.set_armed(True)
            s.flight_mode = "GUIDED"
            phase = t - prearm_dur
            s.alt_rel = phase * climb_rate
            s.vz = -int(climb_rate * 100)  # cm/s, negative = up in NED
        elif t <= prearm_dur + ascent_dur + descent_dur:
            # Descent
            s.set_armed(True)
            s.flight_mode = "GUIDED"
            phase = t - prearm_dur - ascent_dur
            s.alt_rel = max_alt - phase * climb_rate
            s.vz = int(climb_rate * 100)   # cm/s, positive = down in NED
        else:
            # Landed
            s.set_armed(False)
            s.flight_mode = "STABILIZE"
            s.alt_rel = 0.0
            s.vz = 0

        s.alt_amsl = BASE_ALT + s.alt_rel

        # --- GPS track: slow circular orbit while airborne ---
        if s.armed:
            flight_t = t - prearm_dur
            radius_deg = 0.002
            angular_speed = 0.1  # rad/s
            angle = flight_t * angular_speed
            s.lat = BASE_LAT + radius_deg * math.cos(angle)
            s.lon = BASE_LON + radius_deg * math.sin(angle)
            s.heading_deg = (math.degrees(angle) + 90) % 360
        else:
            s.lat = BASE_LAT
            s.lon = BASE_LON
            s.heading_deg = 0.0

        # --- Attitude ---
        if s.armed:
            flight_t = t - prearm_dur
            s.roll = 0.15 * math.sin(flight_t * 0.3)
            s.pitch = math.radians(8.0 + 4.0 * math.sin(flight_t * 0.07))
            s.yaw = math.radians(s.heading_deg)
            s.groundspeed = 5.0 + 2.0 * math.sin(flight_t * 0.1)
            s.airspeed = s.groundspeed + 1.5
            s.throttle = 45 + int(15 * math.sin(flight_t * 0.15))
        else:
            s.roll = 0.0
            s.pitch = 0.0
            s.yaw = 0.0
            s.groundspeed = 0.0
            s.airspeed = 0.0
            s.throttle = 0

        # Gradual battery drain
        elapsed = max(0.0, t - prearm_dur)
        s.battery_pct = max(0, 98 - int(elapsed * 0.05))
        s.voltage = max(20.0, 25.2 - elapsed * 0.003)
        s.current = 15000 + 3000 * math.sin(t * 0.2) if s.armed else 0

        # --- Atmospheric sensor model (altitude-dependent) ----------------
        alt = s.alt_rel
        alt_km = alt / 1000.0

        # Temperature (Kelvin):
        # Standard lapse rate -6.5 C/km from 25 C surface, with a mild
        # inversion layer at 400-500 m (+1.5 C bump) and turbulent noise
        # that increases with altitude.
        surface_temp_c = 25.0
        temp_c = surface_temp_c - 6.5 * alt_km
        # Inversion layer: Gaussian bump centred at 450 m, sigma ~50 m
        inversion = 1.5 * math.exp(-((alt - 450.0) ** 2) / (2 * 50.0 ** 2))
        temp_c += inversion
        # Small turbulent noise, growing with altitude
        turb_amp = 0.2 + 0.3 * alt_km
        temp_c += turb_amp * math.sin(t * 1.7 + alt * 0.01)
        base_temp_k = temp_c + 273.15

        # Humidity (%):
        # Surface ~65%, rises through the boundary layer to ~85% near
        # cloud base (~600 m), then drops above as air dries out aloft.
        # Gaussian peak at 600 m models moisture accumulation at the LCL.
        base_rh = 65.0 - 25.0 * alt_km
        moisture_peak = 22.0 * math.exp(
            -((alt - 600.0) ** 2) / (2 * 120.0 ** 2))
        base_rh += moisture_peak
        base_rh += 3.0 * math.sin(t * 0.8 + alt * 0.007)
        base_rh = max(15.0, min(95.0, base_rh))

        # Per-sensor noise (iMet / HYT probe scatter)
        s.temperature_sensors = [
            base_temp_k + random.uniform(-0.5, 0.5) for _ in range(3)
        ]
        s.humidity_sensors = [
            max(5.0, min(99.0, base_rh + random.uniform(-2, 2)))
            for _ in range(3)
        ]
        s.mean_temp = sum(s.temperature_sensors) / 3.0
        s.mean_rh = sum(s.humidity_sensors) / 3.0

        # Barometric pressure (~0.12 hPa per metre — standard atmosphere)
        s.pressure = 1013.25 - alt * 0.12

        # Wind (altitude-dependent):
        # Logarithmic increase from surface with a low-level jet peak at
        # ~700 m and directional veering (wind backs with height).
        if alt < 2.0:
            wspd = 1.0  # near-surface calm
        else:
            log_profile = 3.0 * math.log(alt / 2.0)
            # Low-level jet: Gaussian peak at 700 m, sigma 150 m
            jet = 5.0 * math.exp(-((alt - 700.0) ** 2) / (2 * 150.0 ** 2))
            wspd = log_profile + jet
            wspd += 0.8 * math.sin(t * 0.5 + alt * 0.005)
            wspd = max(0.5, wspd)
        s.wind_speed = wspd

        # Wind direction: surface 180 deg (south), veers ~30 deg over 1 km
        wind_dir_deg = 180.0 + 30.0 * alt_km + 5.0 * math.sin(t * 0.3)
        s.wind_direction = math.radians(wind_dir_deg % 360)
        s.vertical_wind = -s.vz / 100.0  # NED -> updraft (m/s)

        # Dew point for history (Magnus formula via VehicleState)
        temp_c_mean = s.mean_temp - 273.15
        dew = s.dew_point(temp_c_mean, s.mean_rh)

        # Append history
        s.append_history({
            "time_since_boot": s.time_since_boot,
            "lat": s.lat, "lon": s.lon,
            "alt_rel": s.alt_rel, "alt_amsl": s.alt_amsl,
            "temperature": temp_c_mean, "humidity": s.mean_rh, "dew_temp": dew,
            "wind_speed": s.wind_speed,
            "wind_dir": s.wind_direction,
            "vert_wind": s.vertical_wind,
            "temp_sensors": list(s.temperature_sensors),
            "rh_sensors": list(s.humidity_sensors),
            "vz": s.vz,
        })

        # Drift ADS-B targets with small random walk to keep the map lively
        for tgt in s.adsb_targets.values():
            tgt.lat += random.uniform(-0.0001, 0.0001)
            tgt.lon += random.uniform(-0.0001, 0.0001)
            tgt.heading = (tgt.heading + random.uniform(-2, 2)) % 360
            tgt.last_seen = time.monotonic()
