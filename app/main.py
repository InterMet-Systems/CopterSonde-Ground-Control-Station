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
from app.hud_widget import FlightHUD  # noqa: E402,F401
from app.plot_widget import TimeSeriesPlot, ProfilePlot  # noqa: E402,F401
from app.map_widget import MapWidget  # noqa: E402,F401

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
    """Vehicle command and control: arm/disarm, mode, takeoff/land/RTL, params."""

    def on_arm(self):
        self._confirm("Arm Motors", "Arm the vehicle motors?", self._do_arm)

    def on_disarm(self):
        self._confirm("Disarm Motors", "Disarm the vehicle motors?", self._do_disarm)

    def on_set_mode(self):
        mode = self.ids.mode_spinner.text
        self._confirm("Set Mode", f"Change flight mode to {mode}?",
                      lambda: self._do_set_mode(mode))

    def on_takeoff(self):
        try:
            alt = float(self.ids.takeoff_alt.text)
        except ValueError:
            alt = 10.0
        self._confirm("Takeoff", f"Takeoff to {alt:.0f} m?",
                      lambda: self._do_takeoff(alt))

    def on_land(self):
        self._confirm("Land", "Switch to LAND mode?",
                      lambda: self._do_set_mode("LAND"))

    def on_rtl(self):
        self._confirm("Return to Launch", "Switch to RTL mode?",
                      lambda: self._do_set_mode("RTL"))

    def on_set_param(self):
        name = self.ids.param_name.text.strip()
        try:
            value = float(self.ids.param_value.text)
        except ValueError:
            self.ids.cmd_feedback.text = "Invalid parameter value"
            return
        if not name:
            self.ids.cmd_feedback.text = "Enter a parameter name"
            return
        self._confirm("Set Parameter", f"Set {name} = {value}?",
                      lambda: self._do_set_param(name, value))

    # -- action helpers --

    def _do_arm(self):
        App.get_running_app().mav_client.arm()
        self.ids.cmd_feedback.text = "ARM command sent"

    def _do_disarm(self):
        App.get_running_app().mav_client.disarm()
        self.ids.cmd_feedback.text = "DISARM command sent"

    def _do_set_mode(self, mode):
        App.get_running_app().mav_client.set_mode(mode)
        self.ids.cmd_feedback.text = f"Mode {mode} command sent"

    def _do_takeoff(self, alt):
        App.get_running_app().mav_client.takeoff(alt)
        self.ids.cmd_feedback.text = f"TAKEOFF to {alt:.0f} m sent"

    def _do_set_param(self, name, value):
        App.get_running_app().mav_client.set_param(name, value)
        self.ids.cmd_feedback.text = f"SET {name}={value} sent"

    # -- confirmation popup --

    def _confirm(self, title, message, on_yes):
        from kivy.uix.popup import Popup
        from kivy.uix.label import Label
        from kivy.uix.button import Button

        content = BoxLayout(orientation='vertical', padding=10, spacing=10)
        content.add_widget(Label(
            text=message, font_size='14sp', color=(0.9, 0.9, 0.9, 1)))

        btn_row = BoxLayout(size_hint_y=None, height=44, spacing=10)
        popup = Popup(title=title, content=content,
                      size_hint=(0.6, 0.35), auto_dismiss=False)

        yes_btn = Button(text='Confirm', background_color=(0.2, 0.55, 0.3, 1))
        no_btn = Button(text='Cancel', background_color=(0.5, 0.2, 0.2, 1))

        yes_btn.bind(on_release=lambda *_: (popup.dismiss(), on_yes()))
        no_btn.bind(on_release=lambda *_: popup.dismiss())

        btn_row.add_widget(yes_btn)
        btn_row.add_widget(no_btn)
        content.add_widget(btn_row)
        popup.open()

    def update(self, state):
        if state.armed:
            self.ids.armed_indicator.text = "ARMED"
            self.ids.armed_indicator.color = (0.9, 0.2, 0.2, 1)
        else:
            self.ids.armed_indicator.text = "DISARMED"
            self.ids.armed_indicator.color = (0.3, 0.8, 0.4, 1)
        self.ids.mode_display.text = f"Mode: {state.flight_mode}"


class HUDScreen(Screen):
    """Canvas-drawn flight HUD: attitude, heading, speed/altitude tapes."""

    def update(self, state):
        hud = self.ids.get('hud')
        if hud and state.is_healthy():
            hud.set_state(
                roll=state.roll,
                pitch=state.pitch,
                heading=state.heading_deg,
                airspeed=state.airspeed,
                groundspeed=state.groundspeed,
                alt_rel=state.alt_rel,
                vz=state.vz,
                throttle=state.throttle,
            )


class SensorPlotScreen(Screen):
    """CASS sensor time-series: T1/T2/T3 and RH1/RH2/RH3 vs time."""

    _TEMP_COLORS = [
        (0.9, 0.3, 0.3, 1),   # T1 red
        (0.3, 0.8, 0.3, 1),   # T2 green
        (0.3, 0.5, 0.95, 1),  # T3 blue
    ]
    _RH_COLORS = [
        (0.95, 0.6, 0.2, 1),  # RH1 orange
        (0.5, 0.9, 0.5, 1),   # RH2 light green
        (0.4, 0.7, 0.95, 1),  # RH3 light blue
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._paused = False
        self._snap_time = None
        self._snap_temp = None
        self._snap_rh = None

    def toggle_pause(self):
        self._paused = not self._paused
        btn = self.ids.get('pause_btn')
        if btn:
            btn.text = 'Resume' if self._paused else 'Pause'

    def export_csv(self):
        app = App.get_running_app()
        s = app.vehicle_state
        if not s.h_time:
            return
        import csv
        import os
        if ON_ANDROID:
            base = "/sdcard/CopterSondeGCS"
        else:
            base = os.path.join(_REPO_ROOT, "exports")
        os.makedirs(base, exist_ok=True)
        import datetime
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(base, f"sensors_{ts}.csv")
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time_s", "T1", "T2", "T3", "RH1", "RH2", "RH3"])
            for i, t in enumerate(s.h_time):
                temps = s.h_temp_sensors[i] if i < len(s.h_temp_sensors) else []
                rhs = s.h_rh_sensors[i] if i < len(s.h_rh_sensors) else []
                row = [f"{t:.2f}"]
                row += [f"{v:.2f}" for v in temps] + [""] * (3 - len(temps))
                row += [f"{v:.2f}" for v in rhs] + [""] * (3 - len(rhs))
                writer.writerow(row)
        fb = self.ids.get('export_feedback')
        if fb:
            fb.text = f"Saved: {os.path.basename(path)}"

    def update(self, state):
        if self._paused:
            return
        if not state.h_time:
            return

        # Build temperature series from history
        temp_series = {}
        for idx in range(3):
            name = f"T{idx + 1}"
            color = self._TEMP_COLORS[idx]
            pts = []
            for i, t in enumerate(state.h_time):
                sensors = state.h_temp_sensors[i] if i < len(state.h_temp_sensors) else []
                if idx < len(sensors):
                    pts.append((t, sensors[idx] - 273.15))
            temp_series[name] = (color, pts)

        # Build RH series
        rh_series = {}
        for idx in range(3):
            name = f"RH{idx + 1}"
            color = self._RH_COLORS[idx]
            pts = []
            for i, t in enumerate(state.h_time):
                sensors = state.h_rh_sensors[i] if i < len(state.h_rh_sensors) else []
                if idx < len(sensors):
                    pts.append((t, sensors[idx]))
            rh_series[name] = (color, pts)

        temp_plot = self.ids.get('temp_plot')
        rh_plot = self.ids.get('rh_plot')
        if temp_plot:
            temp_plot.set_data(temp_series)
        if rh_plot:
            rh_plot.set_data(rh_series)


class ProfileScreen(Screen):
    """Temperature, dew point, and wind profiles vs altitude."""

    def clear_profile(self):
        app = App.get_running_app()
        app.vehicle_state.clear_history()
        for pid in ('temp_profile', 'wind_profile'):
            p = self.ids.get(pid)
            if p:
                p.set_data({})

    def update(self, state):
        if not state.h_time:
            return

        import math

        # Temperature & Dew Point vs Altitude
        temp_pts, dew_pts = [], []
        for i, alt in enumerate(state.h_alt_rel):
            if i < len(state.h_temperature):
                temp_pts.append((state.h_temperature[i], alt))
            if i < len(state.h_dew_temp):
                dew_pts.append((state.h_dew_temp[i], alt))

        temp_profile = self.ids.get('temp_profile')
        if temp_profile:
            temp_profile.set_data({
                'Temp': ((0.9, 0.3, 0.3, 1), temp_pts),
                'Dew':  ((0.3, 0.7, 0.95, 1), dew_pts),
            })

        # Wind Speed vs Altitude
        wspd_pts = []
        for i, alt in enumerate(state.h_alt_rel):
            if i < len(state.h_wind_speed):
                wspd_pts.append((state.h_wind_speed[i], alt))

        wind_profile = self.ids.get('wind_profile')
        if wind_profile:
            wind_profile.set_data({
                'Wind Spd': ((0.3, 0.85, 0.5, 1), wspd_pts),
            })


class MapScreen(Screen):
    """Satellite map with drone position, track, and ADS-B targets."""

    def update(self, state):
        m = self.ids.get('map_view')
        if not m:
            return
        if not state.is_healthy():
            return

        # Build track from history
        track = list(zip(state.h_lat, state.h_lon))

        # Build ADS-B target list
        adsb = []
        for tgt in state.adsb_targets.values():
            adsb.append((tgt.callsign, tgt.lat, tgt.lon,
                         tgt.alt_m, tgt.heading))

        m.set_state(
            lat=state.lat, lon=state.lon,
            heading=state.heading_deg,
            track=track, adsb_targets=adsb,
        )


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
