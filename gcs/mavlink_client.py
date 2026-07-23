"""
MAVLink client for CopterSonde GCS.

Runs MAVLink I/O in a background thread so the Kivy UI stays responsive.
Parses all relevant MAVLink messages and populates a shared VehicleState
object.  Emits events via the EventBus for UI subscribers.
"""

import threading
import time

from pymavlink import mavutil
from gcs.message_logger import MessageLogger
from gcs.tlog_writer import TlogWriter
from gcs.raw_message_writer import RawMessageWriter
from gcs.met_balancer import MetBalancer
from gcs.ascent_gate import AscentGate
from gcs.met_derive import WS_A, WS_B, derive, wind_speed, wind_speed_dir
from gcs.met_binner import Binner
from gcs.altitude_level_writer import AltitudeLevelWriter
from gcs.time_interval_writer import TimeIntervalWriter
from gcs.wmo_uas_writer import WmoUasWriter

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
from gcs.log_fetch import LogFetcher
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

# Wind estimation coefficients (SWX quadratic regression formula):
#   wind_h = max(0, ws_a * tan(|pitch|) + ws_b * sqrt(tan(|pitch|)))
# The CopterSonde tilts into the wind; greater pitch => stronger wind.
# WS_A / WS_B (the calibration defaults) and the wind_speed() fit itself are the
# single source of truth in gcs.met_derive and are imported above.  The mutable,
# user-tunable copies live on the instance as self.ws_a / self.ws_b (seeded from
# these defaults, hot-reloaded from Settings).

# ── Remote ID transmission (SoW 205195 §1.11) ──────────────────────────────────
# Operator ID / drone serial come from Settings (self.operator_id /
# self.drone_serial). Operator location is pushed in by the app layer
# from device location services (self.operator_location); None means no
# fix and is broadcast as lat/lon 0/0 ("unknown" per OpenDroneID).
REMOTE_ID_TX_ENABLED = True
RID_SYSTEM_INTERVAL_S = 1.0       # OPEN_DRONE_ID_SYSTEM at 1 Hz
RID_ID_INTERVAL_S = 2.0           # BASIC_ID / OPERATOR_ID at 1/2 Hz
RID_TARGET_SYSTEM = 13
RID_TARGET_COMPONENT = 0
ODID_EPOCH_OFFSET = 1546300800    # Unix seconds at 2019-01-01T00:00:00Z


def _odid_bytes(s, length=20):
    """Encode str/bytes into a fixed-length, null-padded byte field.

    OpenDroneID id_or_mac/uas_id (uint8_t[20]) and operator_id (char[20])
    all pack via struct '20s', which needs a bytes value in Python 3.
    """
    if isinstance(s, str):
        s = s.encode("ascii", errors="replace")
    return s[:length].ljust(length, b"\x00")

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

        # Operator identity, seeded from Settings and hot-reloaded (like ws_a/ws_b)
        self.operator_id = ""
        self.drone_serial = ""

        # Operator (GCS device) location for OPEN_DRONE_ID_SYSTEM, set by
        # the app layer from device location services. None = no fix; a
        # (lat, lon) tuple in decimal degrees is assigned atomically so
        # the IO thread never reads a torn pair.
        self.operator_location = None

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

        # Armed-state debounce: require N consecutive heartbeats with the
        # same armed bit before committing the transition to state.armed.
        # Prevents single corrupted/dropped heartbeats from flickering the
        # armed status and resetting the flight timer.
        self._armed_debounce_count = 0
        self._armed_debounce_value = None
        self._ARMED_DEBOUNCE_N = 3

        # Diagnostics — used by watchdog and elapsed-time displays
        self.msg_count = 0
        self._first_msg_time = None
        self._connect_time = None
        self._last_watchdog_log = 0
        # Per-connection MAVLink message log (one new file per connection)
        self._msg_logger = MessageLogger()
        # One-shot guard so the "OpenDroneID not in dialect" warning logs once
        self._rid_unavailable_logged = False
        self._tlog_writer = TlogWriter()
        self._raw_writer = RawMessageWriter()
        self._balancer = MetBalancer()
        self._log_fetch = LogFetcher(self)
        self._gate = AscentGate(on_start=self._on_ascent_start,
                                on_end=self._on_ascent_end)
        self._alm_binner = Binner(width=5.0, key=lambda r: r.alt_asl)  # altitude-level (5 m)
        self._tim_binner = Binner(width=1.0, key=lambda r: r.time)     # time-interval (1 s)
        self._alm_writer = AltitudeLevelWriter()
        self._tim_writer = TimeIntervalWriter()
        self._wmo_writer = WmoUasWriter()
        # Set at each ascent start; consumed at that ascent's first ascending
        # sample to open the per-ascent message files, keyed to that sample.
        self._open_pending = False

        # Per-message output enables.  A live connection always produces every
        # message (all True), so these never alter the live path.  The replay
        # client sets them from the user's per-message replay-output toggles so
        # a replay can selectively produce RAW / ALM / TIM / WMO.  (The Debug
        # MAVLink dump is gated separately, at MessageLogger.open() time.)
        self._emit_raw = True
        self._emit_alm = True
        self._emit_tim = True
        self._emit_wmo = True

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

        # Mirror all outbound traffic into the per-connection message log.
        # pymavlink invokes this callback from MAVLink.send() after packing,
        # so every *_send() helper (heartbeats, PARAM_SET, RC overrides,
        # stream requests, Remote ID, ...) is captured without touching the
        # individual call sites — including any added in the future.  Sends
        # occur on the IO thread, the UI thread, and worker threads alike;
        # MessageLogger.log_message() is lock-guarded, so this is safe.
        try:
            self._conn.mav.set_send_callback(self._on_message_sent)
        except AttributeError:
            log.warning("pymavlink lacks set_send_callback; "
                        "outbound messages will NOT be logged")

        # Reset diagnostics
        self.msg_count = 0
        self._first_msg_time = None
        self._connect_time = time.monotonic()
        self._last_watchdog_log = 0

        # Fresh balancing + ascent state for this connection -- the autopilot
        # may have rebooted, so the balancer re-syncs its boot-time datums.
        self._balancer.reset()
        self._gate.reset()

        # Start a fresh message log for this connection
        self._msg_logger.open()
        #self._tlog_writer.open()

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
        self._log_fetch.abort("Disconnected")
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self.running = False
        self._streams_requested = False

        # IO thread has stopped — finalize the log with an EOF marker
        self._msg_logger.close()
        self._tlog_writer.close()
        self._raw_writer.close()
        self._alm_writer.close()   # flush a mid-ascent ALM if the ascent never ended cleanly
        self._tim_writer.close()   # likewise for the TIM
        self._wmo_writer.close()   # and the WMO netCDF (writes the accumulated ascent)

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

    def fetch_log(self, on_progress=None, on_done=None):
        """Download the drone's most recent LOG.BIN (SoW #12–#14).

        Runs on the IO thread via the LogFetcher state machine; callbacks
        fire on that thread.  ``on_progress(pct, total_bytes)``,
        ``on_done(success, path_or_reason)``.
        """
        if self._conn is None or not self.running:
            if on_done:
                on_done(False, "Not connected")
            return
        self._log_fetch.start(on_progress, on_done)

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
                if self._conn is None:
                    if on_done:
                        on_done(False, "Arm & Takeoff error: not connected")
                    return

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

        # Per-message Remote ID timers — [builder, interval_s, last_sent].
        # Mutable lists so the firing block can update last_sent in place.
        rid_timers = {
            "system": [self._send_rid_system, RID_SYSTEM_INTERVAL_S, 0.0],
            "operator": [self._send_rid_operator, RID_ID_INTERVAL_S, 0.0],
            "basic": [self._send_rid_basic, RID_ID_INTERVAL_S, 0.0],
        }

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
            # Outside the drain loop: must keep sending even when no
            # telemetry is arriving (mavlink_router needs outbound traffic).
            if now - last_gcs_hb >= GCS_HEARTBEAT_INTERVAL_S:
                self._send_gcs_heartbeat()
                last_gcs_hb = now

            # --- Per-message Remote ID transmission ---
            if REMOTE_ID_TX_ENABLED:
                for entry in rid_timers.values():
                    builder, interval, last = entry
                    if interval > 0 and now - last >= interval:
                        builder()
                        entry[2] = now  # update last_sent in place

            # --- Drone-log download driver (idle -> one state check) ---
            self._log_fetch.tick(now)

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

    def _on_message_sent(self, msg):
        """pymavlink send-callback: mirror one transmitted message (TX)
        into the per-connection message log.

        Runs on whichever thread performed the send.  Must never raise —
        an exception here would propagate out of MAVLink.send() and break
        the transmit path itself.
        """
        try:
            self._msg_logger.log_message(msg, direction="TX")
        except Exception:
            # log_message() has its own failure handling/disable logic, so
            # landing here should be rare; record it without re-raising.
            log.exception("Failed to log outbound message")

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
            self._open_telemetry_log(getattr(msg, "_timestamp", None) or time.time())

        # Log every received message (plumbing; serialization comes later).
        # Outbound messages reach the same log via _on_message_sent().
        self._msg_logger.log_message(msg)
        self._tlog_writer.log_message(msg)

        msg_type = msg.get_type()
        handler = self._MSG_HANDLERS.get(msg_type)
        if handler:
            handler(self, msg)  # unbound method call — self passed explicitly

        # Met pipeline: balance the variable-rate streams into one uniform
        # line.  The raw file logs every balanced line while the aircraft is
        # ARMED (independent of ascent); the profile messages (ALM/TIM/WMO) are
        # gated to ascents.
        #
        # Only the connected vehicle's messages feed the balancer.  The link
        # carries other MAVLink nodes (on Herelink: camera at 42/100, a
        # companion at 42/190, the Remote ID module...) whose 1 Hz HEARTBEATs
        # would otherwise stomp the balancer's carried custom_mode/armed
        # twice a second.  Field failure of 2026-07-06: balanced lines only
        # showed AUTO+armed in the brief window after the drone's own
        # heartbeat, so the ascent gate fired 8 one-line micro-ascents over
        # one real ascent — each start reset the binners, so no bin ever
        # completed: ALM/TIM files were header-only, no WMO file was
        # produced, and the RAW file (armed-gated) got rows only in those
        # windows.  Same bug class _on_heartbeat already filters against.
        # last_sysid/last_compid are set by the first autopilot heartbeat
        # (compid 1, non-GCS); until then no line could carry a valid mode,
        # so skipping the balancer entirely is correct.
        if (self.last_sysid is not None
                and msg.get_srcSystem() == self.last_sysid
                and msg.get_srcComponent() == self.last_compid):
            line = self._balancer.feed(msg)
        else:
            line = None
        if line is not None:
            # Raw file: a row for every balanced line while armed (SoW 205174
            # section 1.6 / 205192 section 5).  The armed flag is carried on the
            # balanced line from the HEARTBEAT, like custom_mode.
            if line.armed:
                self._raw_writer.write_row(line)
            # Profile messages are ascent-gated: only ascending lines feed the
            # derive -> binner -> ALM/TIM/WMO path.
            ascending = self._gate.feed(line)
            if ascending is not None:
                # First ascending sample of this ascent: open the ALM/TIM/WMO
                # files now, keyed to this sample's time (filename + Unix Start
                # Time) and the current Raw file's name (an ALM constant).
                if self._open_pending:
                    if self._emit_alm:
                        self._alm_writer.begin(ascending.time, self._raw_writer.path,
                                               serial=self.drone_serial,
                                               operator_string=self.operator_id)
                    if self._emit_tim:
                        self._tim_writer.begin(ascending.time, self._raw_writer.path,
                                               serial=self.drone_serial,
                                               operator_string=self.operator_id)
                    if self._emit_wmo:
                        self._wmo_writer.begin(ascending.time, self._raw_writer.path,
                                               operator_id=self.operator_id,
                                               airframe_id=self.drone_serial)
                    self._open_pending = False
                # Derive the per-sample level record and bin it two ways:
                # altitude -> altitude-level message, time -> time-interval.
                record = derive(ascending, self.ws_a, self.ws_b)
                alm_bin = self._alm_binner.feed(record)
                if alm_bin is not None:
                    self._alm_writer.write_row(alm_bin)
                    self._wmo_writer.write_row(alm_bin)   # WMO shares the altitude-binned data
                    self._push_alm_plot(alm_bin)
                tim_bin = self._tim_binner.feed(record)
                if tim_bin is not None:
                    self._tim_writer.write_row(tim_bin)

    def _open_telemetry_log(self, start_time):
        """Open the binary telemetry (.tlog) log for this session.

        Called once, from _handle_message, when the first message arrives —
        so the file's YYYYMMDD_HHmmss name marks the start of data arriving
        rather than connect time (SoW #32).  Runs on the MAVLink IO thread;
        TlogWriter's lock makes that safe against the main-thread close().
        The replay client overrides this to skip the .tlog (it still opens the
        RAW file).  ``start_time`` is the first message's UNIX time, forwarded
        to the RAW writer so its filename reflects the recording's time on
        replay.
        """
        self._tlog_writer.open()
        if self._emit_raw:
            self._raw_writer.open(serial=self.drone_serial, start_time=start_time)

    def _push_alm_plot(self, alm_bin):
        """Feed one completed ALM bin to the graphs (SoW 205195 #19).

        Unconditional on the file-output toggles: the bins are computed
        either way, and the graphs are a display, not an output file —
        so a replay with the ALM file disabled still draws its profile.
        Dew point isn't an ALM column; it's derived from the bin's
        temp/RH so the profile plot keeps its dew trace.
        """
        try:
            temp_c = alm_bin.temp - 273.15
            dew_c = self.state.dew_point(temp_c, alm_bin.rh)
            # Wind lives on the record as an e/n vector (averaged linearly by
            # the binner); recover the ALM Wind Speed column the same way the
            # writers do — magnitude of the bin-mean vector.
            wspd = wind_speed_dir(alm_bin)[0]
            self.state.append_alm_bin(alm_bin.time, alm_bin.alt_asl,
                                      temp_c, alm_bin.rh, dew_c, wspd)
        except Exception:
            log.exception("Failed to append ALM bin to plot history")

    def _on_ascent_start(self, n):
        # Fresh bins for each ascent (each ascent is its own profile / file);
        # any partial top bin from the previous ascent is dropped here.  The
        # per-ascent profile files get opened here next.  The ALM plot
        # buffers reset here too (SoW #19: graphs reset when another
        # message begins), so the previous ascent stays on screen until
        # this moment.
        self._alm_binner.reset()
        self._tim_binner.reset()
        self.state.clear_alm()
        self._open_pending = True
        log.info("Ascent #%d started", n)

    def _on_ascent_end(self, n):
        # Close this ascent's ALM file.  The binner never emits its partial
        # top bin, so it never reached the file -- that drop is correct.
        # Clear any unconsumed open-pending so the next ascent starts clean.
        self._alm_writer.close()
        self._tim_writer.close()
        self._wmo_writer.close()
        self._open_pending = False
        log.info("Ascent #%d ended", n)

    def _on_heartbeat(self, msg):
        # Ignore heartbeats from other GCS instances (e.g. QGC)
        if msg.type == mavutil.mavlink.MAV_TYPE_GCS:
            return
        # Only process heartbeats from the autopilot component (compid 1).
        # Other components (companion computers, cameras, gimbals) send
        # heartbeats without the armed bit set, which poisons the armed-
        # state debounce counter and prevents arming detection.
        if msg.get_srcComponent() != mavutil.mavlink.MAV_COMP_ID_AUTOPILOT1:
            return
        now = time.monotonic()
        self.last_heartbeat_time = now
        self.state.last_heartbeat = now
        self.last_sysid = msg.get_srcSystem()
        self.last_compid = msg.get_srcComponent()
        self.vehicle_type = msg.type
        self.autopilot_type = msg.autopilot

        # Debounced armed flag: only transition after N consecutive
        # heartbeats report the same armed state, preventing flicker from
        # single corrupted packets during flight.
        new_armed = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
        if new_armed == self._armed_debounce_value:
            self._armed_debounce_count += 1
        else:
            self._armed_debounce_value = new_armed
            self._armed_debounce_count = 1
        if self._armed_debounce_count >= self._ARMED_DEBOUNCE_N:
            self.state.set_armed(new_armed)

        # Flight mode
        if self._conn is not None:
            try:
                self.state.flight_mode = self._conn.flightmode
            except Exception:
                pass

        self.state.system_status = msg.system_status
        # Raw integer mode (flight_mode above is the decoded name). The raw
        # file's Custom Mode column and ascent detection use this.
        self.state.custom_mode = msg.custom_mode

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

    def _on_local_position_ned(self, msg):
        # LOCAL_POSITION_NED velocities are m/s in the NED frame — distinct
        # from GLOBAL_POSITION_INT's cm/s vx/vy/vz above. The raw file's
        # Velocity columns, ascent detection, ascent rate, and ground speed
        # all use these m/s values.
        self.state.vx_ned = msg.vx   # m/s north
        self.state.vy_ned = msg.vy   # m/s east
        self.state.vz_ned = msg.vz   # m/s down (positive = descending)

    def _on_attitude(self, msg):
        self.state.roll = msg.roll
        self.state.pitch = msg.pitch
        self.state.yaw = msg.yaw
        # Angular rates (rad/s) for the raw file's Roll/Pitch/Yaw Rate columns.
        self.state.rollspeed = msg.rollspeed
        self.state.pitchspeed = msg.pitchspeed
        self.state.yawspeed = msg.yawspeed
        # Recompute wind on every attitude update since wind estimate
        # depends on current pitch angle.
        self._compute_wind()

    def _compute_wind(self):
        """Estimate wind speed and direction from vehicle pitch/yaw.

        Wind speed uses the shared pitch-only fit in ``met_derive.wind_speed``
        with this client's live ``ws_a`` / ``ws_b`` -- the SAME function and
        coefficients the ALM/TIM/WMO derivation uses -- so the live readout and
        the message files always agree.
        Wind direction = vehicle yaw (CopterSonde points into the wind).
        Vertical wind = -vz (vz is cm/s down; positive vertical_wind = updraft).
        """
        self.state.wind_speed = wind_speed(
            self.state.pitch, self.ws_a, self.ws_b)
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
        # convert to milliamps from centiamps
        self.state.current = msg.current_battery * 10 if msg.current_battery >= 0 else 0
        self.state.battery_pct = msg.battery_remaining if msg.battery_remaining >= 0 else 0

    def _on_scaled_pressure2(self, msg):
        # press_abs is hPa, numerically identical to mB (the raw file's
        # Pressure unit), so it is stored as-is. CGCS does not otherwise
        # decode pressure from MAVLink on a live connection.
        self.state.pressure = msg.press_abs

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
        # Monotonic counter for UI cache invalidation — the capped list
        # length below stops changing once it hits 200
        self.state.status_messages_total += 1
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

        _nan = float("nan")

        if dtype == 0:  # Temperature (Kelvin) from iMet probes
            # Filter out invalid/zero readings before averaging
            temps = [v for v in values[:4] if v and v > 0]
            if temps:
                self.state.temperature_sensors = temps
                self.state.mean_temp = sum(temps) / len(temps)
            else:
                self.state.temperature_sensors = []
                self.state.mean_temp = _nan

        elif dtype == 1:  # Relative Humidity (%) from HYT probes
            rhs = [v for v in values[:4] if v and v > 0]
            if rhs:
                self.state.humidity_sensors = rhs
                self.state.mean_rh = sum(rhs) / len(rhs)
            else:
                self.state.humidity_sensors = []
                self.state.mean_rh = _nan

        # dtype 3 (wind) is ignored; wind is computed from pitch via the
        # SWX quadratic formula in _compute_wind().

        # Append history sample on temperature or humidity updates.
        # Use NaN for any sensor group that has no valid readings so the
        # profile and time-series plots skip those points instead of
        # showing bogus 0-values.
        if dtype in (0, 1):
            mean_t = self.state.mean_temp
            # Convert Kelvin to Celsius; use NaN if no valid temperature
            if mean_t == mean_t and mean_t > 100:  # NaN != NaN
                temp_c = mean_t - 273.15
            else:
                temp_c = _nan

            rh = self.state.mean_rh
            # Dew point only meaningful when both temp and RH are valid
            if temp_c == temp_c and rh == rh and rh > 0:
                dew = self.state.dew_point(temp_c, rh)
            else:
                dew = _nan

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
    def _on_log_entry(self, msg):
        self._log_fetch.on_log_entry(msg)

    def _on_log_data(self, msg):
        self._log_fetch.on_log_data(msg)

    _MSG_HANDLERS = {
        "HEARTBEAT":           _on_heartbeat,
        "GLOBAL_POSITION_INT": _on_global_position_int,
        "LOCAL_POSITION_NED":  _on_local_position_ned,
        "ATTITUDE":            _on_attitude,
        "VFR_HUD":             _on_vfr_hud,
        "SYS_STATUS":          _on_sys_status,
        "SCALED_PRESSURE2":    _on_scaled_pressure2,
        "GPS_RAW_INT":         _on_gps_raw_int,
        "RC_CHANNELS":         _on_rc_channels,
        "STATUSTEXT":          _on_statustext,
        "COMMAND_ACK":         _on_command_ack,
        "SERVO_OUTPUT_RAW":    _on_servo_output_raw,
        "ADSB_VEHICLE":        _on_adsb_vehicle,
        "CASS_SENSOR_RAW":     _on_cass_sensor_raw,
        "SYSTEM_TIME":         _on_system_time,
        "PARAM_VALUE":         _on_param_value,
        "LOG_ENTRY":           _on_log_entry,
        "LOG_DATA":            _on_log_data,
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

    def _rid_available(self):
        """True if OpenDroneID send-methods exist in the loaded dialect."""
        conn = self._conn
        if conn is None:
            return False
        if not hasattr(conn.mav, "open_drone_id_system_send"):
            if not self._rid_unavailable_logged:
                self._rid_unavailable_logged = True
                log.warning("Remote ID TX disabled: OpenDroneID messages not "
                            "in this pymavlink dialect — upgrade pymavlink.")
            return False
        return True

    def _send_rid_system(self):
        """OPEN_DRONE_ID_SYSTEM — operator location (the rate-sensitive one)."""
        if not self._rid_available():
            return
        odid_ts = max(0, int(time.time()) - ODID_EPOCH_OFFSET)
        loc = self.operator_location  # single read — atomic snapshot
        if loc is None:
            lat_e7, lon_e7 = 0, 0  # "unknown" per OpenDroneID
        else:
            lat_e7 = int(round(loc[0] * 1e7))
            lon_e7 = int(round(loc[1] * 1e7))
        try:
            self._conn.mav.open_drone_id_system_send(
                RID_TARGET_SYSTEM, RID_TARGET_COMPONENT,
                _odid_bytes(b""),  # id_or_mac (default)
                1,  # operator_location_type
                0,  # classification_type
                lat_e7,  # operator_latitude [degE7]
                lon_e7,  # operator_longitude [degE7]
                1,  # area_count
                0,  # area_radius
                0.0,  # area_ceiling [m]
                0.0,  # area_floor [m]
                0,  # category_eu
                0,  # class_eu
                -1000.0,  # operator_altitude_geo (unknown)
                odid_ts,  # timestamp [s since 2019]
            )
        except Exception:
            log.exception("Failed to send OPEN_DRONE_ID_SYSTEM")

    def _send_rid_operator(self):
        """OPEN_DRONE_ID_OPERATOR_ID."""
        if not self._rid_available():
            return
        try:
            self._conn.mav.open_drone_id_operator_id_send(
                RID_TARGET_SYSTEM, RID_TARGET_COMPONENT,
                _odid_bytes(b""),  # id_or_mac (default)
                0,  # operator_id_type
                _odid_bytes(self.operator_id),  # from settings (SoW #37)
            )
        except Exception:
            log.exception("Failed to send OPEN_DRONE_ID_OPERATOR_ID")

    def _send_rid_basic(self):
        """OPEN_DRONE_ID_BASIC_ID."""
        if not self._rid_available():
            return
        try:
            self._conn.mav.open_drone_id_basic_id_send(
                RID_TARGET_SYSTEM, RID_TARGET_COMPONENT,
                _odid_bytes(b""),  # id_or_mac (default)
                1,  # id_type (serial number)
                2,  # ua_type (multirotor)
                _odid_bytes(self.drone_serial),  # uas_id, from settings (SoW #37)
            )
        except Exception:
            log.exception("Failed to send OPEN_DRONE_ID_BASIC_ID")