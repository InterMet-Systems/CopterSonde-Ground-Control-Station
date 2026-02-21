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
# before any connection is opened — pymavlink uses whichever dialect was
# imported first for all subsequent connections.
try:
    import pymavlink.dialects.v20.all as _dialect  # noqa: F401
except ImportError:
    pass  # fall back to stock ardupilotmega dialect (no CASS messages)

from gcs.logutil import get_logger
from gcs.vehicle_state import VehicleState, ADSBTarget, StatusMessage

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
UDP_BIND_ADDRESS = "0.0.0.0"  # listen on all interfaces
UDP_PORT = 14550              # standard MAVLink GCS port
HEARTBEAT_TIMEOUT_S = 3.0
GCS_HEARTBEAT_INTERVAL_S = 1.0
GCS_SYSID = 255   # conventional sysid for a ground control station
GCS_COMPID = 190   # unique compid to avoid collisions with QGC (190)
DATA_EMIT_INTERVAL_S = 0.1  # 10 Hz data event rate — matches UI refresh
DEFAULT_STREAM_RATE_HZ = 10
# Re-request streams every 5 s to survive autopilot reboots or packet loss
STREAM_REQUEST_INTERVAL_S = 5.0

# MAVLink severity levels (MAV_SEVERITY enum)
SEVERITY_NAMES = {
    0: "EMERGENCY", 1: "ALERT", 2: "CRITICAL", 3: "ERROR",
    4: "WARNING", 5: "NOTICE", 6: "INFO", 7: "DEBUG",
}

# Wind estimation coefficients (SWX quadratic regression formula).
# Derived from CopterSonde calibration against known wind speeds:
#   wind_h = max(0, WS_A * tan(|pitch|) + WS_B * sqrt(tan(|pitch|)))
# The CopterSonde tilts into the wind; greater pitch => stronger wind.
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
        self.event_bus = event_bus  # EventBus for thread-safe UI callbacks

        # Backward-compat convenience aliases — cached from latest HEARTBEAT
        self.last_heartbeat_time = 0.0
        self.last_sysid = None   # source system ID of the vehicle
        self.last_compid = None  # source component ID (usually autopilot=1)
        self.vehicle_type = None
        self.autopilot_type = None

        # Wind estimation coefficients (mutable; user can tweak in Settings UI)
        self.ws_a = WS_A
        self.ws_b = WS_B

        # Internal
        self._conn = None          # pymavlink connection handle
        self._thread = None        # background IO thread
        self._stop_event = threading.Event()  # signals the IO loop to exit
        self.running = False

        # Connection string for pymavlink — format examples:
        #   "udpin:0.0.0.0:14550"  (listen for inbound UDP)
        #   "udpout:192.168.0.10:14550"  (send outbound UDP)
        #   "tcp:192.168.0.10:5760"  (TCP client)
        self._conn_str = None

        # Data stream request tracking — streams are re-requested periodically
        # because the autopilot may reboot or packets may be lost over UDP.
        self.stream_rate_hz = DEFAULT_STREAM_RATE_HZ
        self._streams_requested = False
        self._last_stream_request_time = 0.0

        # Diagnostics — used by watchdog and elapsed-time displays
        self.msg_count = 0
        self._first_msg_time = None
        self._connect_time = None
        self._last_watchdog_log = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, conn_str=None):
        """Open MAVLink connection and start background IO thread."""
        if self.running:
            log.warning("start() called but already running")
            return

        # Default to UDP-in (passive listen) — the autopilot or mavlink_router
        # pushes packets to us on this port.
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

        # Log socket details for diagnostics
        try:
            sock = self._conn.port
            log.info("Socket local address: %s", sock.getsockname())
        except Exception:
            pass

        # Reset diagnostics
        self.msg_count = 0
        self._first_msg_time = None
        self._connect_time = time.monotonic()
        self._last_watchdog_log = 0

        self._stop_event.clear()
        # The IO loop runs in a daemon thread so it is automatically killed
        # if the main process exits, preventing the app from hanging.
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
        self._streams_requested = False
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

    def waiting_elapsed(self):
        """Seconds since start() was called (for UI diagnostics)."""
        if self._connect_time is None:
            return 0.0
        return time.monotonic() - self._connect_time

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

    def set_param(self, name: str, value: float, param_type=None):
        """Set an ArduPilot parameter."""
        if self._conn is None:
            return
        if param_type is None:
            param_type = mavutil.mavlink.MAV_PARAM_TYPE_REAL32
        # MAVLink param names are exactly 16 bytes, null-padded
        name_bytes = name.encode("utf-8").ljust(16, b"\x00")[:16]
        self._conn.mav.param_set_send(
            self.last_sysid or 1,
            self.last_compid or 1,
            name_bytes,
            value,
            param_type,
        )

    def request_all_params(self):
        """Request all parameters from the autopilot via PARAM_REQUEST_LIST."""
        if self._conn is None:
            log.warning("request_all_params: no connection")
            return
        target_sys = self.last_sysid or 1
        target_comp = self.last_compid or 1
        log.info("Requesting all parameters from %d/%d", target_sys, target_comp)
        self._conn.mav.param_request_list_send(target_sys, target_comp)

    def set_rc_override(self, channel: int, pwm_value: int):
        """Override a single RC channel (1-8).

        Used to trigger AutoVP missions — the CopterSonde Lua script
        watches RC7 for a high-PWM signal to start autonomous profiling.
        """
        if self._conn is None:
            return
        target_sys = self.last_sysid or 1
        target_comp = self.last_compid or 1
        rc_values = [0] * 8  # 0 = no change / release for other channels
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
                if self._conn is None:
                    if on_done:
                        on_done(False, "AutoVP error: not connected")
                    return

                # Step 1: Write target altitude to the Lua script's parameter
                log.info("AutoVP: setting USR_AUTOVP_ALT = %.0f", target_altitude)
                self.set_param("USR_AUTOVP_ALT", float(target_altitude))
                time.sleep(0.5)  # allow param to propagate before RC trigger

                # Step 2: Trigger via RC7 channel override — send repeatedly
                # at ~10 Hz for 1.5 s so at least a few packets get through
                # mavlink_router on Herelink (lossy UDP link).
                log.info("AutoVP: sending RC7 override (1900) for 1.5 s")
                t_end = time.monotonic() + 1.5
                while time.monotonic() < t_end:
                    self.set_rc_override(7, 1900)
                    time.sleep(0.1)

                # Step 3: Release RC7 — send multiple times for reliability
                log.info("AutoVP: releasing RC7 override (1100)")
                for _ in range(5):
                    self.set_rc_override(7, 1100)
                    time.sleep(0.1)

                log.info("AutoVP: mission generation triggered")
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
                # Sequence: LOITER (safe hover mode) -> ARM -> AUTO (mission start)
                # Delays between steps give the autopilot time to acknowledge.
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
        """Background IO loop — runs in the 'mavlink-io' daemon thread.

        Architecture: a single tight loop handles receiving, heartbeating,
        stream requests, and event emission.  Sleep at the bottom (5 ms)
        keeps CPU usage low while maintaining sub-10 ms latency.
        """
        from gcs.event_bus import EventType  # cache import outside loop

        # Burst 3 heartbeats at startup to register with mavlink_router
        # on Herelink.  The router will not forward vehicle packets to us
        # until it has seen at least one outbound packet from our GCS.
        for _ in range(3):
            self._send_gcs_heartbeat()
            time.sleep(0.1)
        log.info("Initial heartbeat burst sent (3 packets)")

        last_gcs_hb = 0.0
        last_data_emit = 0.0

        while not self._stop_event.is_set():
            now = time.monotonic()

            # --- Watchdog: log every 5 s while waiting for first message ---
            if self._first_msg_time is None and self._connect_time:
                elapsed = now - self._connect_time
                elapsed_int = int(elapsed)
                if (elapsed_int >= 5
                        and elapsed_int % 5 == 0
                        and elapsed_int != self._last_watchdog_log):
                    self._last_watchdog_log = elapsed_int
                    log.warning(
                        "Still waiting for first MAVLink message… "
                        "(%.0fs elapsed, conn=%s)", elapsed, self._conn_str)

            # --- Receive: drain all pending messages (non-blocking) ---
            # Process every queued packet before sleeping so we don't
            # accumulate latency under high message rates.
            while True:
                try:
                    msg = self._conn.recv_match(blocking=False)
                except Exception:
                    log.exception("recv_match error")
                    msg = None
                if msg is None:
                    break
                self._handle_message(msg)

            # --- Transmit GCS heartbeat at 1 Hz ---
            if now - last_gcs_hb >= GCS_HEARTBEAT_INTERVAL_S:
                self._send_gcs_heartbeat()
                last_gcs_hb = now

            # --- Re-send stream requests periodically ---
            # Handles autopilot reboots or UDP packet loss silently.
            if (self._streams_requested
                    and now - self._last_stream_request_time >= STREAM_REQUEST_INTERVAL_S):
                self._request_data_streams()

            # --- Emit data event at 10 Hz (only if someone is listening) ---
            # has_subscribers() check avoids snapshot() overhead when no
            # UI screen is active (e.g. during settings or param editor).
            if self.event_bus and now - last_data_emit >= DATA_EMIT_INTERVAL_S:
                if self.event_bus.has_subscribers(EventType.DATA_UPDATED):
                    self.event_bus.emit(EventType.DATA_UPDATED,
                                        self.state.snapshot())
                last_data_emit = now

            time.sleep(0.005)  # 5 ms sleep — balances CPU vs. latency

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    def _handle_message(self, msg):
        """Dispatch a single MAVLink message to the appropriate handler.

        Uses a dict-based dispatch table (_MSG_HANDLERS) instead of
        if/elif chains — O(1) lookup and easy to extend with new messages.
        """
        self.msg_count += 1
        if self._first_msg_time is None:
            self._first_msg_time = time.monotonic()
            elapsed = self._first_msg_time - (self._connect_time or self._first_msg_time)
            log.info("First MAVLink message received after %.1fs: %s",
                     elapsed, msg.get_type())

        msg_type = msg.get_type()
        handler = self._MSG_HANDLERS.get(msg_type)
        if handler:
            handler(self, msg)  # unbound method call — self passed explicitly

    def _on_heartbeat(self, msg):
        # Ignore heartbeats from other GCS instances (e.g. QGC)
        if msg.type == mavutil.mavlink.MAV_TYPE_GCS:
            return
        now = time.monotonic()
        self.last_heartbeat_time = now
        self.state.last_heartbeat = now
        self.last_sysid = msg.get_srcSystem()
        self.last_compid = msg.get_srcComponent()
        self.vehicle_type = msg.type
        self.autopilot_type = msg.autopilot

        # Check armed flag via bitmask in base_mode field
        self.state.armed = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)

        # Flight mode
        if self._conn is not None:
            try:
                self.state.flight_mode = self._conn.flightmode
            except Exception:
                pass

        self.state.system_status = msg.system_status

        # Request data streams on first vehicle heartbeat — this tells the
        # autopilot to start sending telemetry at the configured rate.
        if not self._streams_requested:
            self._streams_requested = True
            self._request_data_streams()

    def _on_global_position_int(self, msg):
        # MAVLink sends lat/lon as int32 in degE7 and altitudes in mm
        self.state.lat = msg.lat / 1e7
        self.state.lon = msg.lon / 1e7
        self.state.alt_amsl = msg.alt / 1000.0
        self.state.alt_rel = msg.relative_alt / 1000.0
        self.state.vx = msg.vx   # cm/s
        self.state.vy = msg.vy
        self.state.vz = msg.vz
        if msg.hdg != 65535:  # 65535 = heading unknown
            self.state.heading_deg = msg.hdg / 100.0

    def _on_attitude(self, msg):
        self.state.roll = msg.roll
        self.state.pitch = msg.pitch
        self.state.yaw = msg.yaw
        # Recompute wind on every attitude update since wind estimate
        # depends on current pitch angle.
        self._compute_wind()

    def _compute_wind(self):
        """Estimate wind speed and direction from vehicle pitch/yaw.

        Uses the quadratic formula:
          wind_h = max(0, WS_A * tan(|pitch|) + WS_B * sqrt(tan(|pitch|)))
        Wind direction = vehicle yaw (CopterSonde points into the wind).
        Vertical wind = -vz (vz is cm/s down; positive vertical_wind = updraft).
        """
        pitch = self.state.pitch  # radians
        tan_p = math.tan(abs(pitch))
        if tan_p > 0:
            # SWX quadratic: a*tan(pitch) + b*sqrt(tan(pitch))
            self.state.wind_speed = max(
                0.0, self.ws_a * tan_p + self.ws_b * math.sqrt(tan_p))
        else:
            self.state.wind_speed = 0.0
        # CopterSonde yaw == wind direction (vehicle always points into wind)
        self.state.wind_direction = self.state.yaw
        # vz is cm/s downward (NED frame); negate and convert to get updraft m/s
        self.state.vertical_wind = -self.state.vz / 100.0

    def _on_vfr_hud(self, msg):
        self.state.airspeed = msg.airspeed
        self.state.groundspeed = msg.groundspeed
        self.state.heading_deg = msg.heading
        self.state.throttle = msg.throttle
        # NOTE: VFR_HUD.alt is AMSL, NOT relative to home.
        # Relative altitude is set by _on_global_position_int from
        # GLOBAL_POSITION_INT.relative_alt — do NOT overwrite it here.
        self.state.alt_amsl = msg.alt

    def _on_sys_status(self, msg):
        # voltage_battery is in mV; -1 means not available
        self.state.voltage = msg.voltage_battery / 1000.0 if msg.voltage_battery > 0 else 0
        self.state.current = msg.current_battery if msg.current_battery >= 0 else 0
        self.state.battery_pct = msg.battery_remaining if msg.battery_remaining >= 0 else 0

    def _on_gps_raw_int(self, msg):
        self.state.fix_type = msg.fix_type
        self.state.satellites = msg.satellites_visible
        # eph is HDOP * 100; 9999+ means unknown
        self.state.hdop = msg.eph / 100.0 if msg.eph and msg.eph < 9999 else 99.99

    def _on_rc_channels(self, msg):
        # rssi=255 means "unknown"; 0-254 is the valid range
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
        # Cap status message list to avoid unbounded memory growth
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
        # Upsert target keyed by ICAO address — stale entries are kept until
        # the UI decides to prune them based on last_seen age.
        t = ADSBTarget(
            icao=msg.ICAO_address,
            callsign=msg.callsign.rstrip("\x00"),
            lat=msg.lat / 1e7,
            lon=msg.lon / 1e7,
            alt_m=msg.altitude / 1000.0,   # mm -> m
            heading=msg.heading / 100.0,     # cdeg -> deg
            speed_ms=msg.hor_velocity / 100.0,  # cm/s -> m/s
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

        if dtype == 0:  # Temperature (Kelvin) from iMet probes
            # Filter out invalid/zero readings before averaging
            temps = [v for v in values[:4] if v and v > 0]
            if temps:
                self.state.temperature_sensors = temps
                self.state.mean_temp = sum(temps) / len(temps)

        elif dtype == 1:  # Relative Humidity (%) from HYT probes
            rhs = [v for v in values[:4] if v and v > 0]
            if rhs:
                self.state.humidity_sensors = rhs
                self.state.mean_rh = sum(rhs) / len(rhs)

        # dtype 3 (wind) is ignored; wind is computed from pitch via the
        # SWX quadratic formula in _compute_wind().

        # Append history sample on temperature or humidity updates
        if dtype in (0, 1):
            # Convert Kelvin to Celsius; guard against pre-init values < 100
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

    def _on_param_value(self, msg):
        """Handle incoming PARAM_VALUE message."""
        # param_id may arrive as bytes or str depending on pymavlink version
        param_id = msg.param_id
        if isinstance(param_id, bytes):
            param_id = param_id.decode("utf-8", errors="replace")
        param_id = param_id.rstrip("\x00")  # strip null-padding

        data = {
            "param_id": param_id,
            "param_value": msg.param_value,
            "param_type": msg.param_type,
            "param_index": msg.param_index,
            "param_count": msg.param_count,
        }
        log.debug("PARAM_VALUE: %s = %s (type=%d, %d/%d)",
                  param_id, msg.param_value, msg.param_type,
                  msg.param_index + 1, msg.param_count)

        if self.event_bus:
            from gcs.event_bus import EventType
            self.event_bus.emit(EventType.PARAM_RECEIVED, data)

    def _on_system_time(self, msg):
        self.state.time_since_boot = msg.time_boot_ms / 1000.0
        if msg.time_unix_usec:
            self.state.utc_time = msg.time_unix_usec / 1e6

    # Dispatch table — maps MAVLink message type strings to handler methods.
    # Looked up in _handle_message() for O(1) dispatch.
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
        "PARAM_VALUE":         _on_param_value,
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

    def _request_data_streams(self):
        """Request all data streams from the autopilot at the configured rate.

        Uses the legacy REQUEST_DATA_STREAM message with stream_id 0 (ALL)
        rather than the newer MAV_CMD_SET_MESSAGE_INTERVAL — simpler and
        widely supported by ArduPilot.  Called on first vehicle heartbeat
        and re-sent periodically to recover from autopilot reboots.
        """
        if self._conn is None:
            return
        target_sys = self.last_sysid or 1
        target_comp = self.last_compid or 1
        try:
            self._conn.mav.request_data_stream_send(
                target_sys, target_comp,
                0,                    # MAV_DATA_STREAM_ALL
                self.stream_rate_hz,  # rate in Hz
                1,                    # 1 = start streaming, 0 = stop
            )
        except Exception:
            log.exception("Failed to request data streams")
            return
        self._last_stream_request_time = time.monotonic()
        log.info("Requested all data streams at %d Hz (target %d/%d)",
                 self.stream_rate_hz, target_sys, target_comp)
