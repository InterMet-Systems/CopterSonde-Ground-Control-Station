"""
Centralized vehicle state for CopterSonde GCS.

A single ``VehicleState`` instance holds all telemetry fields parsed from
MAVLink messages.  The MAVLink client (or sim generator) updates this
object; UI screens read from it on the main thread.

Thread-safety note: individual field writes from the IO thread and reads
from the UI thread are safe for Python built-in types (GIL guarantees
atomic reference assignment).  The ``snapshot()`` method returns a plain
dict for convenience.
"""

import math
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class ADSBTarget:
    icao: int = 0
    callsign: str = ""
    lat: float = 0.0
    lon: float = 0.0
    alt_m: float = 0.0
    heading: float = 0.0
    speed_ms: float = 0.0
    last_seen: float = 0.0


@dataclass
class StatusMessage:
    severity: int = 0
    severity_name: str = ""
    text: str = ""
    timestamp: float = 0.0


class VehicleState:
    """Mutable container for all vehicle telemetry."""

    def __init__(self):
        self.reset()

    def reset(self):
        """Clear all fields to defaults."""
        # GPS
        self.lat = 0.0
        self.lon = 0.0
        self.alt_amsl = 0.0
        self.alt_rel = 0.0
        self.fix_type = 0
        self.satellites = 0
        self.hdop = 99.99

        # Attitude (radians)
        self.roll = 0.0
        self.pitch = 0.0
        self.yaw = 0.0

        # Heading (degrees)
        self.heading_deg = 0.0

        # Speed
        self.groundspeed = 0.0
        self.airspeed = 0.0
        self.vx = 0.0  # cm/s north
        self.vy = 0.0  # cm/s east
        self.vz = 0.0  # cm/s down (positive = descending)

        # Battery
        self.voltage = 0.0
        self.current = 0.0  # milliamps
        self.battery_pct = 0

        # Radio
        self.rssi_percent = 0

        # System
        self.armed = False
        self.flight_mode = "---"
        self.system_status = 0
        self.last_heartbeat = 0.0

        # Sensors (CASS)
        self.temperature_sensors: list[float] = []
        self.humidity_sensors: list[float] = []
        self.mean_temp = 0.0       # Kelvin
        self.mean_rh = 0.0         # percent
        self.pressure = 0.0        # hPa

        # Wind (computed)
        self.wind_speed = 0.0      # m/s
        self.wind_direction = 0.0  # radians
        self.vertical_wind = 0.0   # m/s

        # ADS-B
        self.adsb_targets: dict[int, ADSBTarget] = {}

        # Status messages
        self.status_messages: list[StatusMessage] = []

        # Throttle
        self.throttle = 0

        # Timestamps
        self.time_since_boot = 0
        self.utc_time = None

        # Servo / RPM (for wind estimation)
        self.servo_raw: list[int] = [0] * 8

        # History buffers for plots (capped to MAX_HISTORY via deque maxlen)
        self.MAX_HISTORY = 3000
        self._history_keys = [
            "h_time", "h_lat", "h_lon", "h_alt_rel", "h_alt_amsl",
            "h_temperature", "h_humidity", "h_dew_temp",
            "h_wind_speed", "h_wind_dir", "h_vert_wind",
            "h_temp_sensors", "h_rh_sensors", "h_vz",
        ]
        for k in self._history_keys:
            setattr(self, k, deque(maxlen=self.MAX_HISTORY))

    def clear_history(self):
        """Clear only history arrays, keep current-value fields."""
        for k in self._history_keys:
            setattr(self, k, deque(maxlen=self.MAX_HISTORY))

    def heartbeat_age(self):
        if self.last_heartbeat == 0.0:
            return float("inf")
        return time.monotonic() - self.last_heartbeat

    def is_healthy(self):
        return self.heartbeat_age() < 3.0

    def dew_point(self, temp_c, rh):
        """Magnus formula dew-point approximation."""
        if rh <= 0 or temp_c < -50:
            return temp_c - 10.0
        a, b = 17.625, 243.04
        alpha = math.log(rh / 100.0) + a * temp_c / (b + temp_c)
        return (b * alpha) / (a - alpha)

    def append_history(self, data: dict):
        """Append one sample to the rolling history buffers.

        Uses deque(maxlen) so eviction of old samples is O(1).
        """
        self.h_time.append(data.get("time_since_boot", 0))
        self.h_lat.append(data.get("lat", 0))
        self.h_lon.append(data.get("lon", 0))
        self.h_alt_rel.append(data.get("alt_rel", 0))
        self.h_alt_amsl.append(data.get("alt_amsl", 0))
        self.h_temperature.append(data.get("temperature", 0))
        self.h_humidity.append(data.get("humidity", 0))
        self.h_dew_temp.append(data.get("dew_temp", 0))
        self.h_wind_speed.append(data.get("wind_speed", 0))
        self.h_wind_dir.append(data.get("wind_dir", 0))
        self.h_vert_wind.append(data.get("vert_wind", 0))
        self.h_temp_sensors.append(data.get("temp_sensors", []))
        self.h_rh_sensors.append(data.get("rh_sensors", []))
        self.h_vz.append(data.get("vz", 0))

    def snapshot(self) -> dict:
        """Return a plain-dict snapshot of the most commonly needed fields."""
        return {
            "lat": self.lat, "lon": self.lon,
            "alt_amsl": self.alt_amsl, "alt_rel": self.alt_rel,
            "fix_type": self.fix_type, "satellites": self.satellites,
            "hdop": self.hdop,
            "roll": self.roll, "pitch": self.pitch, "yaw": self.yaw,
            "heading_deg": self.heading_deg,
            "groundspeed": self.groundspeed, "airspeed": self.airspeed,
            "vx": self.vx, "vy": self.vy, "vz": self.vz,
            "voltage": self.voltage, "current": self.current,
            "battery_pct": self.battery_pct,
            "rssi_percent": self.rssi_percent,
            "armed": self.armed, "flight_mode": self.flight_mode,
            "mean_temp": self.mean_temp, "mean_rh": self.mean_rh,
            "pressure": self.pressure,
            "wind_speed": self.wind_speed,
            "wind_direction": self.wind_direction,
            "vertical_wind": self.vertical_wind,
            "throttle": self.throttle,
            "time_since_boot": self.time_since_boot,
        }
