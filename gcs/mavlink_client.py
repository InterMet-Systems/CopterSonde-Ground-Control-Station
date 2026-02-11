"""
MAVLink client for CopterSonde GCS.

Runs MAVLink I/O in a background thread so the Kivy UI stays responsive.
Receives vehicle heartbeats, tracks link health, and transmits a GCS
heartbeat at 1 Hz.
"""

import threading
import time

from pymavlink import mavutil

from gcs.logutil import get_logger

# ---------------------------------------------------------------------------
# Configuration  (override before calling start() if needed)
# ---------------------------------------------------------------------------
UDP_BIND_ADDRESS = "0.0.0.0"
UDP_PORT = 14550                  # 14551 for Herelink controller (primary port)
HEARTBEAT_TIMEOUT_S = 3.0        # seconds without heartbeat -> unhealthy
GCS_HEARTBEAT_INTERVAL_S = 1.0   # transmit GCS heartbeat every N seconds
GCS_SYSID = 255                  # standard GCS system ID
GCS_COMPID = 190                 # MAV_COMP_ID_MISSIONPLANNER-ish

log = get_logger("mavlink_client")


class MAVLinkClient:
    """
    Threaded MAVLink UDP client.

    Public read-only state (updated from the IO thread, read from the UI):
        last_heartbeat_time  – monotonic timestamp of last vehicle heartbeat
        last_sysid           – system ID of last heartbeat sender
        last_compid          – component ID of last heartbeat sender
        vehicle_type         – MAV_TYPE from last heartbeat
        autopilot_type       – MAV_AUTOPILOT from last heartbeat
        running              – True while the IO thread is active
    """

    def __init__(self, port=None, bind_address=None):
        self.port = port or UDP_PORT
        self.bind_address = bind_address or UDP_BIND_ADDRESS

        # Link-health state (written by _io_loop, read by UI)
        self.last_heartbeat_time = 0.0
        self.last_sysid = None
        self.last_compid = None
        self.vehicle_type = None
        self.autopilot_type = None

        # Internal
        self._conn = None
        self._thread = None
        self._stop_event = threading.Event()
        self.running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Open the UDP socket and start the background IO thread."""
        if self.running:
            log.warning("start() called but already running")
            return

        conn_str = f"udpin:{self.bind_address}:{self.port}"
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

    def heartbeat_age(self):
        """Seconds since the last vehicle heartbeat (monotonic)."""
        if self.last_heartbeat_time == 0.0:
            return float("inf")
        return time.monotonic() - self.last_heartbeat_time

    def is_healthy(self):
        """True when a vehicle heartbeat was received recently."""
        return self.heartbeat_age() < HEARTBEAT_TIMEOUT_S

    # ------------------------------------------------------------------
    # Background IO loop
    # ------------------------------------------------------------------

    def _io_loop(self):
        """Run in a daemon thread.  Receives messages and sends GCS HB."""
        last_gcs_hb = 0.0

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

            # Short sleep to avoid busy-spin while keeping latency low
            time.sleep(0.02)

    def _handle_message(self, msg):
        """Process an incoming MAVLink message."""
        msg_type = msg.get_type()

        if msg_type == "HEARTBEAT":
            # Ignore heartbeats from other GCS stations
            if msg.type == mavutil.mavlink.MAV_TYPE_GCS:
                return

            self.last_heartbeat_time = time.monotonic()
            self.last_sysid = msg.get_srcSystem()
            self.last_compid = msg.get_srcComponent()
            self.vehicle_type = msg.type
            self.autopilot_type = msg.autopilot

            log.debug(
                "HEARTBEAT from sysid=%d compid=%d type=%d autopilot=%d",
                self.last_sysid,
                self.last_compid,
                self.vehicle_type,
                self.autopilot_type,
            )

    def _send_gcs_heartbeat(self):
        """Transmit a MAVLink HEARTBEAT identifying us as a GCS."""
        try:
            self._conn.mav.heartbeat_send(
                mavutil.mavlink.MAV_TYPE_GCS,
                mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                0,   # base_mode – not applicable for GCS
                0,   # custom_mode
                mavutil.mavlink.MAV_STATE_ACTIVE,
            )
        except Exception:
            log.exception("Failed to send GCS heartbeat")
