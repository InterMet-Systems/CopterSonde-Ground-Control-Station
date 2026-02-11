"""
CopterSonde Ground Control Station – Kivy application entry point.

Multi-screen GCS app with bottom navigation bar.
"""

import json
import os
import sys

# ---------------------------------------------------------------------------
# Ensure the repo root is on sys.path
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ---------------------------------------------------------------------------
# Kivy configuration – must come BEFORE any other kivy import
# ---------------------------------------------------------------------------
from kivy.config import Config  # noqa: E402

Config.set("graphics", "width", "960")
Config.set("graphics", "height", "540")
Config.set("graphics", "resizable", "1")

from kivy.app import App  # noqa: E402
from kivy.clock import Clock  # noqa: E402
from kivy.lang import Builder  # noqa: E402
from kivy.uix.boxlayout import BoxLayout  # noqa: E402
from kivy.uix.screenmanager import ScreenManager, Screen, SlideTransition  # noqa: E402
from kivy.properties import StringProperty, ListProperty  # noqa: E402

from gcs.logutil import setup_logging, get_logger  # noqa: E402
from gcs.event_bus import EventBus, EventType  # noqa: E402
from gcs.vehicle_state import VehicleState  # noqa: E402
from gcs.mavlink_client import MAVLinkClient  # noqa: E402
from gcs.sim_telemetry import SimTelemetry  # noqa: E402

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
try:
    import android  # noqa: F401
    ON_ANDROID = True
    DEFAULT_PORT = 14551
except ImportError:
    ON_ANDROID = False
    DEFAULT_PORT = 14550

UI_UPDATE_HZ = 4

setup_logging()
log = get_logger("app")

# ---------------------------------------------------------------------------
# Settings persistence path
# ---------------------------------------------------------------------------
def _settings_path():
    if ON_ANDROID:
        return "/sdcard/CopterSondeGCS/settings.json"
    return os.path.join(_REPO_ROOT, "settings.json")


def _load_settings():
    p = _settings_path()
    if os.path.exists(p):
        try:
            with open(p, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_settings(data):
    p = _settings_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(data, f, indent=2)


# KV file path — loaded after class definitions below
_KV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.kv")


# ═══════════════════════════════════════════════════════════════════════════
# Root widget (defined in app.kv)
# ═══════════════════════════════════════════════════════════════════════════

class GCSRoot(BoxLayout):
    """Root widget containing the ScreenManager and bottom nav bar."""
    pass


# ═══════════════════════════════════════════════════════════════════════════
# Connection Screen
# ═══════════════════════════════════════════════════════════════════════════

class ConnectionScreen(Screen):
    """Connection management: transport selection, connect/disconnect, demo mode."""

    def on_enter(self):
        # Load saved settings into UI
        app = App.get_running_app()
        settings = app.settings_data
        self.ids.ip_input.text = settings.get("last_ip", "0.0.0.0")
        self.ids.port_input.text = str(settings.get("last_port", DEFAULT_PORT))

    def on_connect_toggle(self):
        app = App.get_running_app()
        if app.mav_client.running or app.sim.running:
            self._disconnect(app)
        else:
            self._connect(app)

    def on_demo_toggle(self, active):
        app = App.get_running_app()
        if active:
            # Stop real connection if running
            if app.mav_client.running:
                app.mav_client.stop()
            app.sim.start()
            self.ids.connect_btn.text = "Stop Demo"
            self.ids.connect_btn.background_color = (0.6, 0.2, 0.2, 1)
            self._start_ui_refresh(app)
        else:
            app.sim.stop()
            self.ids.connect_btn.text = "Connect"
            self.ids.connect_btn.background_color = (0.2, 0.55, 0.3, 1)
            self._stop_ui_refresh(app)
            self._set_status("Not Connected", (0.7, 0.2, 0.2, 1), "Disconnected")

    def _connect(self, app):
        ip = self.ids.ip_input.text.strip() or "0.0.0.0"
        port = self.ids.port_input.text.strip() or str(DEFAULT_PORT)

        # Persist settings
        app.settings_data["last_ip"] = ip
        app.settings_data["last_port"] = int(port)
        _save_settings(app.settings_data)

        conn_str = f"udpin:{ip}:{port}"
        try:
            app.mav_client.start(conn_str=conn_str)
        except Exception as exc:
            log.error("Connection failed: %s", exc)
            self._set_status("Connection Error", (0.9, 0.4, 0.1, 1), str(exc))
            return

        self.ids.connect_btn.text = "Disconnect"
        self.ids.connect_btn.background_color = (0.6, 0.2, 0.2, 1)
        self._start_ui_refresh(app)

    def _disconnect(self, app):
        app.mav_client.stop()
        app.sim.stop()
        self._stop_ui_refresh(app)
        self.ids.connect_btn.text = "Connect"
        self.ids.connect_btn.background_color = (0.2, 0.55, 0.3, 1)
        self.ids.demo_toggle.active = False
        self._set_status("Not Connected", (0.7, 0.2, 0.2, 1), "Disconnected")

    def _start_ui_refresh(self, app):
        if app.update_event is None:
            app.update_event = Clock.schedule_interval(
                app.update_ui, 1.0 / UI_UPDATE_HZ
            )

    def _stop_ui_refresh(self, app):
        if app.update_event is not None:
            app.update_event.cancel()
            app.update_event = None

    def _set_status(self, status, color, detail):
        self.ids.status_label.text = status
        self.ids.status_label.color = color
        self.ids.detail_label.text = detail

    def update(self, state):
        """Called periodically from the app update loop."""
        if state.is_healthy():
            self._set_status(
                "Healthy", (0.15, 0.75, 0.3, 1),
                f"HB age: {state.heartbeat_age():.1f}s | "
                f"Mode: {state.flight_mode} | "
                f"{'ARMED' if state.armed else 'DISARMED'}"
            )
        elif state.last_heartbeat > 0:
            self._set_status(
                "No Heartbeat", (0.9, 0.6, 0.1, 1),
                f"Last heartbeat: {state.heartbeat_age():.1f}s ago"
            )
        else:
            self._set_status(
                "Waiting…", (0.7, 0.2, 0.2, 1),
                "No vehicle heartbeat received yet."
            )


# ═══════════════════════════════════════════════════════════════════════════
# Reusable telemetry tile widget
# ═══════════════════════════════════════════════════════════════════════════

_TILE_DEFAULT = [0.18, 0.18, 0.22, 1]
_TILE_GREEN = [0.12, 0.45, 0.2, 1]
_TILE_YELLOW = [0.55, 0.5, 0.1, 1]
_TILE_RED = [0.6, 0.15, 0.15, 1]

GPS_FIX_NAMES = {
    0: "NO GPS", 1: "NO FIX", 2: "2D FIX",
    3: "3D FIX", 4: "DGPS", 5: "RTK FLT", 6: "RTK FIX",
}


class TelemetryTile(BoxLayout):
    """Reusable tile widget for displaying a labeled telemetry value."""
    label_text = StringProperty('')
    value_text = StringProperty('---')
    tile_color = ListProperty([0.18, 0.18, 0.22, 1])


# ═══════════════════════════════════════════════════════════════════════════
# Telemetry Screen
# ═══════════════════════════════════════════════════════════════════════════

class TelemetryScreen(Screen):
    """Grouped telemetry tiles with threshold color-coding."""

    def update(self, state):
        if not state.is_healthy():
            return

        # --- System ---
        self.ids.tile_mode.value_text = state.flight_mode
        self.ids.tile_armed.value_text = "ARMED" if state.armed else "DISARMED"
        self.ids.tile_armed.tile_color = (
            _TILE_RED if state.armed else _TILE_DEFAULT
        )

        t = int(state.time_since_boot)
        m, s = divmod(t, 60)
        h, m = divmod(m, 60)
        self.ids.tile_time.value_text = f"{h:02d}:{m:02d}:{s:02d}"

        # --- Battery ---
        self.ids.tile_batt_pct.value_text = f"{state.battery_pct}%"
        if state.battery_pct >= 50:
            self.ids.tile_batt_pct.tile_color = _TILE_GREEN
        elif state.battery_pct >= 30:
            self.ids.tile_batt_pct.tile_color = _TILE_YELLOW
        else:
            self.ids.tile_batt_pct.tile_color = _TILE_RED

        self.ids.tile_voltage.value_text = f"{state.voltage:.1f} V"
        self.ids.tile_current.value_text = f"{state.current / 1000:.1f} A"

        # --- Navigation ---
        self.ids.tile_alt_rel.value_text = f"{state.alt_rel:.1f} m"
        self.ids.tile_alt_amsl.value_text = f"{state.alt_amsl:.1f} m"
        self.ids.tile_heading.value_text = f"{state.heading_deg:.0f}\u00b0"

        # --- Speed ---
        self.ids.tile_gndspd.value_text = f"{state.groundspeed:.1f} m/s"
        self.ids.tile_airspd.value_text = f"{state.airspeed:.1f} m/s"
        vz_ms = state.vz / 100.0
        self.ids.tile_vertspd.value_text = f"{vz_ms:.1f} m/s"

        # --- GPS ---
        fix_name = GPS_FIX_NAMES.get(state.fix_type, f"TYPE {state.fix_type}")
        self.ids.tile_gps_fix.value_text = fix_name
        if state.fix_type >= 3:
            self.ids.tile_gps_fix.tile_color = _TILE_GREEN
        elif state.fix_type >= 2:
            self.ids.tile_gps_fix.tile_color = _TILE_YELLOW
        else:
            self.ids.tile_gps_fix.tile_color = _TILE_RED

        self.ids.tile_sats.value_text = str(state.satellites)
        if state.satellites >= 10:
            self.ids.tile_sats.tile_color = _TILE_GREEN
        elif state.satellites >= 6:
            self.ids.tile_sats.tile_color = _TILE_YELLOW
        else:
            self.ids.tile_sats.tile_color = _TILE_RED

        self.ids.tile_hdop.value_text = f"{state.hdop:.1f}"
        if state.hdop < 2.0:
            self.ids.tile_hdop.tile_color = _TILE_GREEN
        elif state.hdop < 3.0:
            self.ids.tile_hdop.tile_color = _TILE_YELLOW
        else:
            self.ids.tile_hdop.tile_color = _TILE_RED

        # --- Radio & Throttle ---
        self.ids.tile_rssi.value_text = f"{state.rssi_percent}%"
        if state.rssi_percent >= 70:
            self.ids.tile_rssi.tile_color = _TILE_GREEN
        elif state.rssi_percent >= 40:
            self.ids.tile_rssi.tile_color = _TILE_YELLOW
        else:
            self.ids.tile_rssi.tile_color = _TILE_RED

        self.ids.tile_throttle.value_text = f"{state.throttle}%"

        # --- Last update timestamp ---
        self.ids.last_update_label.text = f"HB: {state.heartbeat_age():.1f}s ago"

    def copy_snapshot(self):
        """Copy current telemetry values to the system clipboard as text."""
        app = App.get_running_app()
        s = app.vehicle_state
        vz_ms = s.vz / 100.0
        fix = GPS_FIX_NAMES.get(s.fix_type, f"TYPE {s.fix_type}")
        t = int(s.time_since_boot)
        mi, sec = divmod(t, 60)
        hr, mi = divmod(mi, 60)
        lines = [
            "=== CopterSonde Telemetry Snapshot ===",
            f"Mode: {s.flight_mode}  Armed: {'YES' if s.armed else 'NO'}",
            f"Time: {hr:02d}:{mi:02d}:{sec:02d}",
            f"Battery: {s.battery_pct}%  {s.voltage:.1f}V  {s.current/1000:.1f}A",
            f"Alt Rel: {s.alt_rel:.1f}m  AMSL: {s.alt_amsl:.1f}m",
            f"Heading: {s.heading_deg:.0f}\u00b0",
            f"GndSpd: {s.groundspeed:.1f}  AirSpd: {s.airspeed:.1f}  VSpd: {vz_ms:.1f} m/s",
            f"GPS: {fix}  Sats: {s.satellites}  HDOP: {s.hdop:.1f}",
            f"RSSI: {s.rssi_percent}%  Throttle: {s.throttle}%",
        ]
        try:
            from kivy.core.clipboard import Clipboard
            Clipboard.copy("\n".join(lines))
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# Placeholder screens (to be implemented in later features)
# ═══════════════════════════════════════════════════════════════════════════


class CommandScreen(Screen):
    def update(self, state):
        pass


class HUDScreen(Screen):
    def update(self, state):
        pass


class SensorPlotScreen(Screen):
    def update(self, state):
        pass


class ProfileScreen(Screen):
    def update(self, state):
        pass


class MapScreen(Screen):
    def update(self, state):
        pass


class MonitoringScreen(Screen):
    def update(self, state):
        pass


class SettingsScreen(Screen):
    def update(self, state):
        pass


# ---------------------------------------------------------------------------
# Load KV — all Screen classes must be defined above this point so the KV
# parser can resolve them.
# ---------------------------------------------------------------------------
Builder.load_file(_KV_PATH)


# ═══════════════════════════════════════════════════════════════════════════
# App
# ═══════════════════════════════════════════════════════════════════════════

class CopterSondeGCSApp(App):
    title = "CopterSonde GCS"

    def build(self):
        self.settings_data = _load_settings()

        # Shared state and event bus
        self.event_bus = EventBus()
        self.vehicle_state = VehicleState()

        # MAVLink client
        self.mav_client = MAVLinkClient(
            port=DEFAULT_PORT,
            state=self.vehicle_state,
            event_bus=self.event_bus,
        )

        # Sim telemetry
        self.sim = SimTelemetry(
            state=self.vehicle_state,
            event_bus=self.event_bus,
        )

        # UI update event handle
        self.update_event = None

        root = GCSRoot()
        return root

    def on_start(self):
        """Called after build — the widget tree from KV is ready."""
        sm = self.root.ids.sm
        sm.transition = SlideTransition(duration=0.2)
        sm.add_widget(ConnectionScreen(name="connection"))
        sm.add_widget(TelemetryScreen(name="telemetry"))
        sm.add_widget(CommandScreen(name="command"))
        sm.add_widget(HUDScreen(name="hud"))
        sm.add_widget(SensorPlotScreen(name="sensor_plots"))
        sm.add_widget(ProfileScreen(name="profile"))
        sm.add_widget(MapScreen(name="map"))
        sm.add_widget(MonitoringScreen(name="monitoring"))
        sm.add_widget(SettingsScreen(name="settings"))
        self.sm = sm

    def switch_screen(self, name):
        self.root.ids.sm.current = name

    def update_ui(self, _dt):
        """Periodic UI refresh — delegates to the current screen."""
        screen = self.sm.current_screen
        if hasattr(screen, "update"):
            screen.update(self.vehicle_state)

    def on_pause(self):
        return True

    def on_resume(self):
        pass

    def on_stop(self):
        log.info("Application stopping – cleaning up…")
        if self.update_event:
            self.update_event.cancel()
        self.mav_client.stop()
        self.sim.stop()


def main():
    CopterSondeGCSApp().run()


if __name__ == "__main__":
    main()
