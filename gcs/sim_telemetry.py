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

# Base position – Norman, OK
BASE_LAT = 35.2226
BASE_LON = -97.4395
BASE_ALT = 357.0  # AMSL meters

# SWX-Q wind estimation coefficients (same as mavlink_client)
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

        # Sim clock
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
        """Set initial plausible values."""
        s = self.state
        s.lat = BASE_LAT
        s.lon = BASE_LON
        s.alt_amsl = BASE_ALT
        s.alt_rel = 0.0
        s.fix_type = 3
        s.satellites = 14
        s.hdop = 0.95
        s.voltage = 25.2
        s.current = 0
        s.battery_pct = 98
        s.rssi_percent = 85
        s.armed = False
        s.flight_mode = "STABILIZE"
        s.last_heartbeat = time.monotonic()

        # Seed a few ADS-B targets
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
        s = self.state
        s.last_heartbeat = time.monotonic()
        s.time_since_boot = t

        # --- Simulate a circular flight pattern after 10s ---
        if t > 10.0:
            s.armed = True
            s.flight_mode = "GUIDED"
            phase = t - 10.0

            # Ascend for 30s, then orbit
            if phase < 30.0:
                s.alt_rel = phase * 3.0  # climb at 3 m/s
                s.vz = -300  # cm/s ascending (negative = up in NED)
            else:
                s.alt_rel = 90.0 + 10.0 * math.sin(phase * 0.05)
                s.vz = int(10.0 * math.cos(phase * 0.05) * 100 * 0.05)

            s.alt_amsl = BASE_ALT + s.alt_rel

            # Circular GPS track
            radius_deg = 0.002
            angular_speed = 0.1  # rad/s
            angle = phase * angular_speed
            s.lat = BASE_LAT + radius_deg * math.cos(angle)
            s.lon = BASE_LON + radius_deg * math.sin(angle)
            s.heading_deg = (math.degrees(angle) + 90) % 360

            # Attitude — pitch varies ~5-12 deg to produce realistic wind
            s.roll = 0.15 * math.sin(phase * 0.3)
            s.pitch = math.radians(8.0 + 4.0 * math.sin(phase * 0.07))
            s.yaw = math.radians(s.heading_deg)

            # Speed
            s.groundspeed = 5.0 + 2.0 * math.sin(phase * 0.1)
            s.airspeed = s.groundspeed + 1.5

            # Battery drain
            s.battery_pct = max(0, 98 - int(phase * 0.05))
            s.voltage = 25.2 - phase * 0.003
            s.current = 15000 + 3000 * math.sin(phase * 0.2)

            s.throttle = 45 + int(15 * math.sin(phase * 0.15))

        else:
            s.armed = False
            s.flight_mode = "STABILIZE"

        # --- Simulated CASS sensor data ---
        base_temp_k = 293.15 + 2.0 * math.sin(t * 0.05)  # ~20°C
        base_rh = 55.0 + 10.0 * math.sin(t * 0.03)

        s.temperature_sensors = [
            base_temp_k + random.uniform(-0.5, 0.5),
            base_temp_k + random.uniform(-0.5, 0.5),
            base_temp_k + random.uniform(-0.5, 0.5),
        ]
        s.humidity_sensors = [
            base_rh + random.uniform(-2, 2),
            base_rh + random.uniform(-2, 2),
            base_rh + random.uniform(-2, 2),
        ]
        s.mean_temp = sum(s.temperature_sensors) / 3.0
        s.mean_rh = sum(s.humidity_sensors) / 3.0
        s.pressure = 1013.25 - s.alt_rel * 0.12

        # Wind — SWX quadratic formula from pitch angle
        tan_p = math.tan(abs(s.pitch))
        if tan_p > 0:
            s.wind_speed = max(
                0.0, self.ws_a * tan_p + self.ws_b * math.sqrt(tan_p))
        else:
            s.wind_speed = 0.0
        s.wind_direction = s.yaw
        s.vertical_wind = -s.vz / 100.0

        # Dew point for history
        temp_c = s.mean_temp - 273.15
        dew = s.dew_point(temp_c, s.mean_rh)

        # Append history
        s.append_history({
            "time_since_boot": s.time_since_boot,
            "lat": s.lat, "lon": s.lon,
            "alt_rel": s.alt_rel, "alt_amsl": s.alt_amsl,
            "temperature": temp_c, "humidity": s.mean_rh, "dew_temp": dew,
            "wind_speed": s.wind_speed,
            "wind_dir": s.wind_direction,
            "vert_wind": s.vertical_wind,
            "temp_sensors": list(s.temperature_sensors),
            "rh_sensors": list(s.humidity_sensors),
            "vz": s.vz,
        })

        # Move ADS-B targets slowly
        for tgt in s.adsb_targets.values():
            tgt.lat += random.uniform(-0.0001, 0.0001)
            tgt.lon += random.uniform(-0.0001, 0.0001)
            tgt.heading = (tgt.heading + random.uniform(-2, 2)) % 360
            tgt.last_seen = time.monotonic()
