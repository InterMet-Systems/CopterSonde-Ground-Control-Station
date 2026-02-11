"""
MAVLink client for CopterSonde GCS.

Runs MAVLink I/O in a background thread so the Kivy UI stays responsive.
Parses all relevant MAVLink messages and populates a shared VehicleState
object.  Emits events via the EventBus for UI subscribers.
"""

import math
import threading
import time

from pymavlink import mavutil

# Load custom MAVLink dialect that includes CASS_SENSOR_RAW (msg 227).
# The custom pymavlink fork from tony2157/my-mavlink embeds these definitions
# in the ardupilotmega dialect.  Importing v20.all ensures it is registered
# before any connection is opened.
try:
    import pymavlink.dialects.v20.all as _dialect  # noqa: F401
except ImportError:
    pass

from gcs.logutil import get_logger
from gcs.vehicle_state import VehicleState, ADSBTarget, StatusMessage

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
UDP_BIND_ADDRESS = "0.0.0.0"
UDP_PORT = 14550
HEARTBEAT_TIMEOUT_S = 3.0
GCS_HEARTBEAT_INTERVAL_S = 1.0
GCS_SYSID = 255
GCS_COMPID = 190
DATA_EMIT_INTERVAL_S = 0.1  # 10 Hz data event rate

SEVERITY_NAMES = {
    0: "EMERGENCY", 1: "ALERT", 2: "CRITICAL", 3: "ERROR",
    4: "WARNING", 5: "NOTICE", 6: "INFO", 7: "DEBUG",
}

# SWX-Q wind estimation coefficients (quadratic regression formula)
# Derived from CopterSonde calibration against known wind speeds.
# wind_h = max(0, WS_A * tan(|pitch|) + WS_B * sqrt(tan(|pitch|)))
WS_A = 37.1
WS_B = 3.8

log = get_logger("mavlink_client")


class MAVLinkClient:
    """
    Threaded MAVLink UDP client.

    Populates a ``VehicleState`` and optionally emits events via an
    ``EventBus``.
    """

    def __init__(self, port=None, bind_address=None, state=None, event_bus=None):
        self.port = port or UDP_PORT
        self.bind_address = bind_address or UDP_BIND_ADDRESS
        self.state: VehicleState = state or VehicleState()
        self.event_bus = event_bus

        # Backward-compat convenience aliases
        self.last_heartbeat_time = 0.0
        self.last_sysid = None
        self.last_compid = None
        self.vehicle_type = None
        self.autopilot_type = None

        # Wind estimation coefficients (mutable; updated from Settings)
        self.ws_a = WS_A
        self.ws_b = WS_B

        # Internal
        self._conn = None
        self._thread = None
        self._stop_event = threading.Event()
        self.running = False

        # Connection string (for reconnect)
        self._conn_str = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, conn_str=None):
        """Open MAVLink connection and start background IO thread."""
        if self.running:
            log.warning("start() called but already running")
            return

        if conn_str is None:
            conn_str = f"udpin:{self.bind_address}:{self.port}"
        self._conn_str = conn_str

        log.info("Opening MAVLink connection: %s", conn_str)
        try:
            self._conn = mavutil.mavlink_connection(
                conn_str,
                source_system=GCS_SYSID,
                source_component=GCS_COMPID,
            )
        except Exception:
            log.exception("Failed to open MAVLink connection")
            raise

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._io_loop, name="mavlink-io", daemon=True
        )
        self._thread.start()
        self.running = True
        log.info("MAVLink IO thread started")

        if self.event_bus:
            from gcs.event_bus import EventType
            self.event_bus.emit(EventType.CONNECTION_CHANGED,
                                {"connected": True})

    def stop(self):
        """Signal the IO thread to stop and wait for it to finish."""
        if not self.running:
            return
        log.info("Stopping MAVLink IO thread …")
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self.running = False
        log.info("MAVLink IO thread stopped")

        if self.event_bus:
            from gcs.event_bus import EventType
            self.event_bus.emit(EventType.CONNECTION_CHANGED,
                                {"connected": False})

    def heartbeat_age(self):
        if self.last_heartbeat_time == 0.0:
            return float("inf")
        return time.monotonic() - self.last_heartbeat_time

    def is_healthy(self):
        return self.heartbeat_age() < HEARTBEAT_TIMEOUT_S

    # ------------------------------------------------------------------
    # Command helpers
    # ------------------------------------------------------------------

    def send_command_long(self, command, p1=0, p2=0, p3=0, p4=0, p5=0, p6=0, p7=0):
        """Send a MAV_CMD via COMMAND_LONG."""
        if self._conn is None:
            return
        target_sys = self.last_sysid or 1
        target_comp = self.last_compid or 1
        self._conn.mav.command_long_send(
            target_sys, target_comp,
            command, 0,
            p1, p2, p3, p4, p5, p6, p7,
        )

    def arm(self):
        self.send_command_long(
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, p1=1
        )

    def disarm(self):
        self.send_command_long(
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, p1=0
        )

    def set_mode(self, mode_name: str):
        """Set flight mode by name (e.g. 'GUIDED', 'RTL', 'LAND')."""
        if self._conn is None:
            return
        mode_map = self._conn.mode_mapping()
        if mode_name.upper() in mode_map:
            mode_id = mode_map[mode_name.upper()]
            self._conn.set_mode(mode_id)
        else:
            log.warning("Unknown mode: %s", mode_name)

    def takeoff(self, alt_m: float = 10.0):
        self.send_command_long(
            mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
            p7=alt_m,
        )

    def set_param(self, name: str, value: float):
        """Set an ArduPilot parameter."""
        if self._conn is None:
            return
        name_bytes = name.encode("utf-8").ljust(16, b"\x00")[:16]
        self._conn.mav.param_set_send(
            self.last_sysid or 1,
            self.last_compid or 1,
            name_bytes,
            value,
            mavutil.mavlink.MAV_PARAM_TYPE_REAL32,
        )

    def set_rc_override(self, channel: int, pwm_value: int):
        """Override a single RC channel (1-8)."""
        if self._conn is None:
            return
        target_sys = self.last_sysid or 1
        target_comp = self.last_compid or 1
        rc_values = [0] * 8  # 0 = no change / release
        rc_values[channel - 1] = pwm_value
        self._conn.mav.rc_channels_override_send(
            target_sys, target_comp, *rc_values
        )

    def trigger_autovp(self, target_altitude: float, on_done=None):
        """Write target altitude param and trigger AutoVP via RC7.

        Runs in a background thread.  Calls ``on_done(success, message)``
        on completion.
        """
        def _worker():
            try:
                # Write USR_AUTOVP_ALT parameter
                self.set_param("USR_AUTOVP_ALT", float(target_altitude))
                time.sleep(0.5)

                # Trigger via RC7 channel override
                self.set_rc_override(7, 1900)
                time.sleep(1.0)
                self.set_rc_override(7, 1100)

                if on_done:
                    on_done(True,
                            f"AutoVP triggered: {target_altitude:.0f} m")
            except Exception as exc:
                log.exception("trigger_autovp failed")
                if on_done:
                    on_done(False, f"AutoVP error: {exc}")

        threading.Thread(target=_worker, name="autovp-trigger",
                         daemon=True).start()

    def arm_and_takeoff_auto(self, on_done=None):
        """Arm and start Auto mission: LOITER -> ARM -> AUTO.

        Runs in a background thread.  Calls ``on_done(success, message)``
        on completion.
        """
        def _worker():
            try:
                self.set_mode("LOITER")
                time.sleep(2.0)
                self.arm()
                time.sleep(3.0)
                self.set_mode("AUTO")

                if on_done:
                    on_done(True, "Armed — Auto mission started")
            except Exception as exc:
                log.exception("arm_and_takeoff_auto failed")
                if on_done:
                    on_done(False, f"Arm & Takeoff error: {exc}")

        threading.Thread(target=_worker, name="arm-takeoff",
                         daemon=True).start()

    # ------------------------------------------------------------------
    # Background IO loop
    # ------------------------------------------------------------------

    def _io_loop(self):
        last_gcs_hb = 0.0
        last_data_emit = 0.0

        while not self._stop_event.is_set():
            now = time.monotonic()

            # --- Receive ---
            try:
                msg = self._conn.recv_match(blocking=False)
            except Exception:
                log.exception("recv_match error")
                msg = None

            if msg is not None:
                self._handle_message(msg)

            # --- Transmit GCS heartbeat at 1 Hz ---
            if now - last_gcs_hb >= GCS_HEARTBEAT_INTERVAL_S:
                self._send_gcs_heartbeat()
                last_gcs_hb = now

            # --- Emit data event at 10 Hz ---
            if self.event_bus and now - last_data_emit >= DATA_EMIT_INTERVAL_S:
                from gcs.event_bus import EventType
                self.event_bus.emit(EventType.DATA_UPDATED,
                                    self.state.snapshot())
                last_data_emit = now

            time.sleep(0.005)

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    def _handle_message(self, msg):
        msg_type = msg.get_type()
        handler = self._MSG_HANDLERS.get(msg_type)
        if handler:
            handler(self, msg)

    def _on_heartbeat(self, msg):
        if msg.type == mavutil.mavlink.MAV_TYPE_GCS:
            return
        now = time.monotonic()
        self.last_heartbeat_time = now
        self.state.last_heartbeat = now
        self.last_sysid = msg.get_srcSystem()
        self.last_compid = msg.get_srcComponent()
        self.vehicle_type = msg.type
        self.autopilot_type = msg.autopilot

        # Armed?
        self.state.armed = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)

        # Flight mode
        if self._conn is not None:
            try:
                self.state.flight_mode = self._conn.flightmode
            except Exception:
                pass

        self.state.system_status = msg.system_status

    def _on_global_position_int(self, msg):
        self.state.lat = msg.lat / 1e7
        self.state.lon = msg.lon / 1e7
        self.state.alt_amsl = msg.alt / 1000.0
        self.state.alt_rel = msg.relative_alt / 1000.0
        self.state.vx = msg.vx   # cm/s
        self.state.vy = msg.vy
        self.state.vz = msg.vz
        if msg.hdg != 65535:
            self.state.heading_deg = msg.hdg / 100.0

    def _on_attitude(self, msg):
        self.state.roll = msg.roll
        self.state.pitch = msg.pitch
        self.state.yaw = msg.yaw
        self._compute_wind()

    def _compute_wind(self):
        """Estimate wind speed and direction from vehicle pitch/yaw.

        Uses the SWX-Q quadratic formula:
          wind_h = max(0, WS_A * tan(|pitch|) + WS_B * sqrt(tan(|pitch|)))
        Wind direction = vehicle yaw (CopterSonde points into the wind).
        Vertical wind = -vz (vz is cm/s down; positive vertical_wind = updraft).
        """
        pitch = self.state.pitch
        tan_p = math.tan(abs(pitch))
        if tan_p > 0:
            self.state.wind_speed = max(
                0.0, self.ws_a * tan_p + self.ws_b * math.sqrt(tan_p))
        else:
            self.state.wind_speed = 0.0
        self.state.wind_direction = self.state.yaw
        self.state.vertical_wind = -self.state.vz / 100.0

    def _on_vfr_hud(self, msg):
        self.state.airspeed = msg.airspeed
        self.state.groundspeed = msg.groundspeed
        self.state.heading_deg = msg.heading
        self.state.throttle = msg.throttle
        self.state.alt_rel = msg.alt
        # climb is m/s up
        # store as vz in cm/s down for consistency
        # Actually keep climb in a friendlier field:
        pass

    def _on_sys_status(self, msg):
        self.state.voltage = msg.voltage_battery / 1000.0 if msg.voltage_battery > 0 else 0
        self.state.current = msg.current_battery if msg.current_battery >= 0 else 0
        self.state.battery_pct = msg.battery_remaining if msg.battery_remaining >= 0 else 0

    def _on_gps_raw_int(self, msg):
        self.state.fix_type = msg.fix_type
        self.state.satellites = msg.satellites_visible
        self.state.hdop = msg.eph / 100.0 if msg.eph and msg.eph < 9999 else 99.99

    def _on_rc_channels(self, msg):
        if hasattr(msg, "rssi") and msg.rssi < 255:
            self.state.rssi_percent = int(msg.rssi * 100 / 254)

    def _on_statustext(self, msg):
        sm = StatusMessage(
            severity=msg.severity,
            severity_name=SEVERITY_NAMES.get(msg.severity, "UNKNOWN"),
            text=msg.text,
            timestamp=time.time(),
        )
        self.state.status_messages.append(sm)
        if len(self.state.status_messages) > 200:
            self.state.status_messages = self.state.status_messages[-200:]
        log.info("STATUSTEXT [%s]: %s", sm.severity_name, sm.text)

    def _on_command_ack(self, msg):
        log.info("COMMAND_ACK cmd=%d result=%d", msg.command, msg.result)

    def _on_servo_output_raw(self, msg):
        self.state.servo_raw = [
            msg.servo1_raw, msg.servo2_raw, msg.servo3_raw, msg.servo4_raw,
            msg.servo5_raw, msg.servo6_raw, msg.servo7_raw, msg.servo8_raw,
        ]

    def _on_adsb_vehicle(self, msg):
        t = ADSBTarget(
            icao=msg.ICAO_address,
            callsign=msg.callsign.rstrip("\x00"),
            lat=msg.lat / 1e7,
            lon=msg.lon / 1e7,
            alt_m=msg.altitude / 1000.0,
            heading=msg.heading / 100.0,
            speed_ms=msg.hor_velocity / 100.0,
            last_seen=time.monotonic(),
        )
        self.state.adsb_targets[t.icao] = t

    def _on_cass_sensor_raw(self, msg):
        """Handle custom CASS_SENSOR_RAW (msg 227).

        Multiplexed message — ``app_datatype`` selects the payload:
          0 = iMet temperatures (K) in values[0..3]
          1 = HYT humidity (%)     in values[0..3]
          2 = iMet resistance      in values[0..3]  (not used here)
          3 = Wind data: dir=values[0], speed=values[1]
        """
        dtype = getattr(msg, "app_datatype", None)
        values = getattr(msg, "values", None)
        if dtype is None or values is None:
            return

        # Update boot time from message timestamp
        boot_ms = getattr(msg, "time_boot_ms", 0)
        if boot_ms:
            self.state.time_since_boot = boot_ms / 1000.0

        if dtype == 0:  # Temperature (Kelvin)
            temps = [v for v in values[:4] if v and v > 0]
            if temps:
                self.state.temperature_sensors = temps
                self.state.mean_temp = sum(temps) / len(temps)

        elif dtype == 1:  # Relative Humidity (%)
            rhs = [v for v in values[:4] if v and v > 0]
            if rhs:
                self.state.humidity_sensors = rhs
                self.state.mean_rh = sum(rhs) / len(rhs)

        # dtype 3 (wind) is ignored; wind is computed from pitch via the
        # SWX quadratic formula in _compute_wind().

        # Append history sample on temperature or humidity updates
        if dtype in (0, 1):
            temp_c = ((self.state.mean_temp - 273.15)
                      if self.state.mean_temp > 100
                      else self.state.mean_temp)
            rh = self.state.mean_rh
            dew = self.state.dew_point(temp_c, rh)
            self.state.append_history({
                "time_since_boot": self.state.time_since_boot,
                "lat": self.state.lat, "lon": self.state.lon,
                "alt_rel": self.state.alt_rel,
                "alt_amsl": self.state.alt_amsl,
                "temperature": temp_c, "humidity": rh, "dew_temp": dew,
                "wind_speed": self.state.wind_speed,
                "wind_dir": self.state.wind_direction,
                "vert_wind": self.state.vertical_wind,
                "temp_sensors": list(self.state.temperature_sensors),
                "rh_sensors": list(self.state.humidity_sensors),
                "vz": self.state.vz,
            })

    def _on_system_time(self, msg):
        self.state.time_since_boot = msg.time_boot_ms / 1000.0
        if msg.time_unix_usec:
            self.state.utc_time = msg.time_unix_usec / 1e6

    # Dispatch table
    _MSG_HANDLERS = {
        "HEARTBEAT":           _on_heartbeat,
        "GLOBAL_POSITION_INT": _on_global_position_int,
        "ATTITUDE":            _on_attitude,
        "VFR_HUD":             _on_vfr_hud,
        "SYS_STATUS":          _on_sys_status,
        "GPS_RAW_INT":         _on_gps_raw_int,
        "RC_CHANNELS":         _on_rc_channels,
        "STATUSTEXT":          _on_statustext,
        "COMMAND_ACK":         _on_command_ack,
        "SERVO_OUTPUT_RAW":    _on_servo_output_raw,
        "ADSB_VEHICLE":        _on_adsb_vehicle,
        "CASS_SENSOR_RAW":     _on_cass_sensor_raw,
        "SYSTEM_TIME":         _on_system_time,
    }

    def _send_gcs_heartbeat(self):
        try:
            self._conn.mav.heartbeat_send(
                mavutil.mavlink.MAV_TYPE_GCS,
                mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                0, 0,
                mavutil.mavlink.MAV_STATE_ACTIVE,
            )
        except Exception:
            log.exception("Failed to send GCS heartbeat")
