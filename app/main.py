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
from kivy.uix.tabbedpanel import TabbedPanel, TabbedPanelItem  # noqa: E402,F401
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
from app.theme import get_color, set_theme, get_theme_name, THEME_NAMES  # noqa: E402

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
try:
    import android  # noqa: F401
    ON_ANDROID = True
    DEFAULT_PORT = 14552
    DEFAULT_IP = "127.0.0.1"
    DEFAULT_CONN_TYPE = "udpout"
except ImportError:
    ON_ANDROID = False
    DEFAULT_PORT = 14550
    DEFAULT_IP = "0.0.0.0"
    DEFAULT_CONN_TYPE = "udpin"

CONN_TYPES = ["udpin", "udpout", "tcp"]

# Connection presets — (display_name, conn_type, ip, port)
CONNECTION_PRESETS = [
    ("Herelink (UDP out 14552)", "udpout", "127.0.0.1", "14552"),
    ("Herelink (UDP out 14551)", "udpout", "127.0.0.1", "14551"),
    ("Herelink (UDP in 14550)",  "udpin",  "0.0.0.0",   "14550"),
    ("Desktop (UDP in 14550)",   "udpin",  "0.0.0.0",   "14550"),
    ("Custom", "", "", ""),
]
PRESET_NAMES = [p[0] for p in CONNECTION_PRESETS]
PRESET_MAP = {p[0]: p[1:] for p in CONNECTION_PRESETS}

UI_UPDATE_HZ = 4

setup_logging()
log = get_logger("app")

# ---------------------------------------------------------------------------
# Settings persistence path
# ---------------------------------------------------------------------------
def _android_storage_base():
    """Return the user-visible storage base on Android, with fallback."""
    try:
        from android.storage import primary_external_storage_path  # type: ignore
        return os.path.join(primary_external_storage_path(), "CopterSondeGCS")
    except Exception:
        pass
    try:
        from android.storage import app_storage_path  # type: ignore
        return os.path.join(app_storage_path(), "CopterSondeGCS")
    except Exception:
        pass
    return "/sdcard/CopterSondeGCS"


def _settings_path():
    if ON_ANDROID:
        return os.path.join(_android_storage_base(), "settings", "settings.json")
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
        self.ids.conn_type_spinner.text = settings.get("last_conn_type", DEFAULT_CONN_TYPE)
        self.ids.ip_input.text = settings.get("last_ip", DEFAULT_IP)
        self.ids.port_input.text = str(settings.get("last_port", DEFAULT_PORT))
        # Set default preset based on platform
        preset_spinner = self.ids.get("preset_spinner")
        if preset_spinner and preset_spinner.text == "":
            preset_spinner.text = settings.get(
                "last_preset",
                "Herelink (UDP out 14552)" if ON_ANDROID else "Desktop (UDP in 14550)",
            )

    def on_preset_changed(self, preset_name):
        """Auto-populate connection fields from the selected preset."""
        preset = PRESET_MAP.get(preset_name)
        if preset is None or not preset[0]:
            return  # "Custom" — leave fields as-is
        conn_type, ip, port = preset
        self.ids.conn_type_spinner.text = conn_type
        self.ids.ip_input.text = ip
        self.ids.port_input.text = port

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
            self.ids.connect_btn.background_color = list(get_color("btn_disconnect"))
            self._start_ui_refresh(app)
        else:
            app.sim.stop()
            self.ids.connect_btn.text = "Connect"
            self.ids.connect_btn.background_color = list(get_color("btn_connect"))
            self._stop_ui_refresh(app)
            self._set_status("Not Connected", get_color("status_error"), "Disconnected")

    def _connect(self, app):
        conn_type = self.ids.conn_type_spinner.text or DEFAULT_CONN_TYPE
        ip = self.ids.ip_input.text.strip() or DEFAULT_IP
        port = self.ids.port_input.text.strip() or str(DEFAULT_PORT)

        # Persist settings
        app.settings_data["last_conn_type"] = conn_type
        app.settings_data["last_ip"] = ip
        app.settings_data["last_port"] = int(port)
        preset_spinner = self.ids.get("preset_spinner")
        if preset_spinner:
            app.settings_data["last_preset"] = preset_spinner.text
        _save_settings(app.settings_data)

        conn_str = f"{conn_type}:{ip}:{port}"
        self._set_status(
            "Connecting…", get_color("status_warn"),
            f"Connecting via {conn_str}…")

        try:
            app.mav_client.start(conn_str=conn_str)
        except Exception as exc:
            log.error("Connection failed: %s", exc)
            self._set_status("Connection Error", get_color("status_conn_err"), str(exc))
            return

        self.ids.connect_btn.text = "Disconnect"
        self.ids.connect_btn.background_color = list(get_color("btn_disconnect"))
        self.ids.demo_toggle.disabled = True
        self._start_ui_refresh(app)

    def _disconnect(self, app):
        app.mav_client.stop()
        app.sim.stop()
        self._stop_ui_refresh(app)
        self.ids.connect_btn.text = "Connect"
        self.ids.connect_btn.background_color = list(get_color("btn_connect"))
        self.ids.demo_toggle.active = False
        self.ids.demo_toggle.disabled = False
        self._set_status("Not Connected", get_color("status_error"), "Disconnected")

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
        app = App.get_running_app()
        if state.is_healthy():
            self._set_status(
                "Healthy", get_color("status_healthy"),
                f"HB age: {state.heartbeat_age():.1f}s | "
                f"Mode: {state.flight_mode} | "
                f"{'ARMED' if state.armed else 'DISARMED'}"
            )
        elif state.last_heartbeat > 0:
            self._set_status(
                "No Heartbeat", get_color("status_warn"),
                f"Last heartbeat: {state.heartbeat_age():.1f}s ago"
            )
        elif app.mav_client.running:
            # Show diagnostic info while waiting for first message
            elapsed = app.mav_client.waiting_elapsed()
            msgs = app.mav_client.msg_count
            detail = f"Waiting for heartbeat… ({elapsed:.0f}s, {msgs} msgs)"
            if elapsed > 15:
                detail += "  — No response. Try a different preset."
            self._set_status("Waiting…", get_color("status_error"), detail)
        else:
            self._set_status(
                "Not Connected", get_color("status_error"),
                "Configure connection below"
            )


# ═══════════════════════════════════════════════════════════════════════════
# Reusable telemetry tile widget
# ═══════════════════════════════════════════════════════════════════════════

def _tile_color(name):
    return list(get_color(name))

GPS_FIX_NAMES = {
    0: "NO GPS", 1: "NO FIX", 2: "2D FIX",
    3: "3D FIX", 4: "DGPS", 5: "RTK FLT", 6: "RTK FIX",
}


class TelemetryTile(BoxLayout):
    """Reusable tile widget for displaying a labeled telemetry value."""
    label_text = StringProperty('')
    value_text = StringProperty('---')
    tile_color = ListProperty([0.18, 0.18, 0.22, 1])  # default; overridden by theme


# ═══════════════════════════════════════════════════════════════════════════
# Pre-flight checklist items
# ═══════════════════════════════════════════════════════════════════════════

CHECKLIST_ITEMS = [
    "Good weather and air traffic",
    "Battery installation",
    "Confirm good health status of the CopterSonde",
    "KP solar storm index lower than 5",
    "CopterSonde is place on the launch pad",
    "Mission is generated",
    "Approval from crew for flights",
]


# ═══════════════════════════════════════════════════════════════════════════
# Unified Flight Screen (telemetry + HUD + commands)
# ═══════════════════════════════════════════════════════════════════════════

class FlightScreen(Screen):
    """Unified flight screen: telemetry table (left half), HUD (top-right),
    commands with pre-flight checklist (bottom-right)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._checklist_complete = False
        self._checklist_popup = None
        self._proceed_btn = None
        self._check_states = {}
        self._prev_armed = None  # track armed state transitions

    # ── Telemetry update ──────────────────────────────────────────────

    def _update_telemetry(self, state):
        if not state.is_healthy():
            return

        # System
        self.ids.tile_mode.value_text = state.flight_mode
        self.ids.tile_armed.value_text = "ARMED" if state.armed else "DISARMED"
        self.ids.tile_armed.tile_color = (
            _tile_color("tile_green") if state.armed else _tile_color("tile_red")
        )

        t = int(state.time_since_boot)
        m, s = divmod(t, 60)
        h, m = divmod(m, 60)
        self.ids.tile_time.value_text = f"{h:02d}:{m:02d}:{s:02d}"

        # Battery
        self.ids.tile_batt_pct.value_text = f"{state.battery_pct}%"
        if state.battery_pct >= 50:
            self.ids.tile_batt_pct.tile_color = _tile_color("tile_green")
        elif state.battery_pct >= 30:
            self.ids.tile_batt_pct.tile_color = _tile_color("tile_yellow")
        else:
            self.ids.tile_batt_pct.tile_color = _tile_color("tile_red")

        self.ids.tile_voltage.value_text = f"{state.voltage:.1f} V"
        self.ids.tile_current.value_text = f"{state.current / 1000:.1f} A"

        # Navigation
        self.ids.tile_alt_rel.value_text = f"{state.alt_rel:.1f} m"
        self.ids.tile_alt_amsl.value_text = f"{state.alt_amsl:.1f} m"
        self.ids.tile_heading.value_text = f"{state.heading_deg:.0f}\u00b0"

        # Speed
        self.ids.tile_gndspd.value_text = f"{state.groundspeed:.1f} m/s"
        vz_ms = state.vz / 100.0
        self.ids.tile_vertspd.value_text = f"{vz_ms:.1f} m/s"

        # GPS
        fix_name = GPS_FIX_NAMES.get(state.fix_type, f"TYPE {state.fix_type}")
        self.ids.tile_gps_fix.value_text = fix_name
        if state.fix_type >= 3:
            self.ids.tile_gps_fix.tile_color = _tile_color("tile_green")
        elif state.fix_type >= 2:
            self.ids.tile_gps_fix.tile_color = _tile_color("tile_yellow")
        else:
            self.ids.tile_gps_fix.tile_color = _tile_color("tile_red")

        self.ids.tile_sats.value_text = str(state.satellites)
        if state.satellites >= 10:
            self.ids.tile_sats.tile_color = _tile_color("tile_green")
        elif state.satellites >= 6:
            self.ids.tile_sats.tile_color = _tile_color("tile_yellow")
        else:
            self.ids.tile_sats.tile_color = _tile_color("tile_red")

        self.ids.tile_hdop.value_text = f"{state.hdop:.1f}"
        if state.hdop < 2.0:
            self.ids.tile_hdop.tile_color = _tile_color("tile_green")
        elif state.hdop < 3.0:
            self.ids.tile_hdop.tile_color = _tile_color("tile_yellow")
        else:
            self.ids.tile_hdop.tile_color = _tile_color("tile_red")

        # Radio & Throttle
        self.ids.tile_rssi.value_text = f"{state.rssi_percent}%"
        if state.rssi_percent >= 70:
            self.ids.tile_rssi.tile_color = _tile_color("tile_green")
        elif state.rssi_percent >= 40:
            self.ids.tile_rssi.tile_color = _tile_color("tile_yellow")
        else:
            self.ids.tile_rssi.tile_color = _tile_color("tile_red")

        self.ids.tile_throttle.value_text = f"{state.throttle}%"

    # ── HUD update ────────────────────────────────────────────────────

    def _update_hud(self, state):
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

    # ── Command: mission generator ────────────────────────────────────

    def on_generate_mission(self):
        try:
            alt = float(self.ids.vp_altitude.text)
        except ValueError:
            self.ids.cmd_feedback.text = "Invalid altitude value"
            return
        if alt < 20 or alt > 1500:
            self.ids.cmd_feedback.text = "Altitude must be 20\u20131500 m"
            return
        self._confirm("Generate Mission",
                      f"Generate vertical profile mission to {alt:.0f} m?",
                      lambda: self._do_generate_mission(alt))

    def _do_generate_mission(self, alt):
        app = App.get_running_app()
        self.ids.cmd_feedback.text = f"Generating mission ({alt:.0f} m)\u2026"

        def _on_done(success, message):
            Clock.schedule_once(
                lambda _dt: setattr(self.ids.cmd_feedback, 'text', message), 0)

        app.mav_client.trigger_autovp(alt, on_done=_on_done)

    # ── Command: arm & takeoff ────────────────────────────────────────

    def on_arm(self):
        if not self._checklist_complete:
            self.ids.cmd_feedback.text = "Complete pre-flight checklist first"
            return
        self._confirm("Arm & Takeoff (Auto)",
                      "ARM and start AUTO mission?",
                      self._do_arm_takeoff)

    def _do_arm_takeoff(self):
        app = App.get_running_app()
        self.ids.cmd_feedback.text = "Arming: LOITER \u2192 ARM \u2192 AUTO\u2026"

        def _on_done(success, message):
            Clock.schedule_once(
                lambda _dt: setattr(self.ids.cmd_feedback, 'text', message), 0)

        app.mav_client.arm_and_takeoff_auto(on_done=_on_done)

    # ── Command: loiter (replaces LAND) ───────────────────────────────

    def on_loiter(self):
        self._confirm("Loiter", "Switch to LOITER mode?",
                      lambda: self._do_set_mode("LOITER"))

    # ── Command: RTL ──────────────────────────────────────────────────

    def on_rtl(self):
        self._confirm("Return to Launch", "Switch to RTL mode?",
                      lambda: self._do_set_mode("RTL"))

    def _do_set_mode(self, mode):
        App.get_running_app().mav_client.set_mode(mode)
        self.ids.cmd_feedback.text = f"Mode {mode} command sent"

    # ── Confirmation popup ────────────────────────────────────────────

    def _confirm(self, title, message, on_yes):
        from kivy.uix.popup import Popup
        from kivy.uix.label import Label
        from kivy.uix.button import Button

        content = BoxLayout(orientation='vertical', padding=10, spacing=10)
        content.add_widget(Label(
            text=message, font_size='14sp', color=get_color("text_label")))

        btn_row = BoxLayout(size_hint_y=None, height=44, spacing=10)
        popup = Popup(title=title, content=content,
                      size_hint=(0.6, 0.35), auto_dismiss=False)

        yes_btn = Button(text='Confirm', background_color=list(get_color("btn_connect")))
        no_btn = Button(text='Cancel', background_color=list(get_color("btn_clear")))

        yes_btn.bind(on_release=lambda *_: (popup.dismiss(), on_yes()))
        no_btn.bind(on_release=lambda *_: popup.dismiss())

        btn_row.add_widget(yes_btn)
        btn_row.add_widget(no_btn)
        content.add_widget(btn_row)
        popup.open()

    # ── Pre-flight checklist popup ────────────────────────────────────

    def on_checklist(self):
        app = App.get_running_app()
        if app.vehicle_state.armed:
            self.ids.cmd_feedback.text = "Cannot open checklist while armed"
            return
        self._show_checklist_popup()

    def _show_checklist_popup(self):
        from kivy.uix.popup import Popup
        from kivy.uix.label import Label
        from kivy.uix.button import Button
        from kivy.uix.checkbox import CheckBox
        from kivy.uix.scrollview import ScrollView

        content = BoxLayout(orientation='vertical', padding=10, spacing=8)

        content.add_widget(Label(
            text='Complete all items before flight',
            font_size='14sp', size_hint_y=None, height=30,
            color=get_color("text_label")))

        scroll = ScrollView(do_scroll_y=True, do_scroll_x=False)
        checklist_box = BoxLayout(
            orientation='vertical', size_hint_y=None, spacing=6,
            padding=[0, 4, 0, 4])
        checklist_box.bind(minimum_height=checklist_box.setter('height'))

        self._check_states = {}
        for i, item_text in enumerate(CHECKLIST_ITEMS):
            row = BoxLayout(size_hint_y=None, height=36, spacing=8)
            cb = CheckBox(size_hint_x=None, width=36, active=False)
            lbl = Label(
                text=item_text, font_size='12sp',
                color=get_color("text_primary"),
                halign='left', valign='middle')
            lbl.bind(size=lambda inst, val: setattr(
                inst, 'text_size', (inst.width, None)))
            self._check_states[i] = cb
            cb.bind(active=lambda inst, val: self._update_proceed_btn())
            row.add_widget(cb)
            row.add_widget(lbl)
            checklist_box.add_widget(row)

        scroll.add_widget(checklist_box)
        content.add_widget(scroll)

        btn_row = BoxLayout(size_hint_y=None, height=44, spacing=10)
        proceed_btn = Button(
            text='Proceed', font_size='14sp',
            background_color=list(get_color("btn_connect")),
            disabled=True)
        cancel_btn = Button(
            text='Cancel', font_size='14sp',
            background_color=list(get_color("btn_clear")))

        self._proceed_btn = proceed_btn

        popup = Popup(
            title='Pre-Flight Checklist', content=content,
            size_hint=(0.7, 0.8), auto_dismiss=False)

        proceed_btn.bind(
            on_release=lambda *_: self._on_checklist_proceed(popup))
        cancel_btn.bind(
            on_release=lambda *_: self._on_checklist_cancel(popup))

        btn_row.add_widget(proceed_btn)
        btn_row.add_widget(cancel_btn)
        content.add_widget(btn_row)

        self._checklist_popup = popup
        popup.open()

    def _update_proceed_btn(self):
        if self._proceed_btn:
            all_checked = all(
                cb.active for cb in self._check_states.values())
            self._proceed_btn.disabled = not all_checked

    def _on_checklist_proceed(self, popup):
        self._checklist_complete = True
        self.ids.arm_btn.disabled = False
        popup.dismiss()
        self._checklist_popup = None
        self._proceed_btn = None
        self.ids.cmd_feedback.text = "Checklist complete \u2014 ARM & TAKEOFF enabled"

    def _on_checklist_cancel(self, popup):
        popup.dismiss()
        self._checklist_popup = None
        self._proceed_btn = None

    # ── Armed state management ────────────────────────────────────────

    def _update_armed_state(self, state):
        armed = state.armed
        if armed == self._prev_armed:
            return

        if armed:
            # Transitioning to ARMED — disable checklist and arm buttons
            self.ids.checklist_btn.disabled = True
            self.ids.arm_btn.disabled = True
            if self._checklist_popup:
                self._checklist_popup.dismiss()
                self._checklist_popup = None
                self._proceed_btn = None
        else:
            # Transitioning to DISARMED — re-enable checklist, reset
            self.ids.checklist_btn.disabled = False
            self._checklist_complete = False
            self.ids.arm_btn.disabled = True

        self._prev_armed = armed

    # ── Copy snapshot ─────────────────────────────────────────────────

    def copy_snapshot(self):
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
            f"GndSpd: {s.groundspeed:.1f}  VSpd: {vz_ms:.1f} m/s",
            f"GPS: {fix}  Sats: {s.satellites}  HDOP: {s.hdop:.1f}",
            f"RSSI: {s.rssi_percent}%  Throttle: {s.throttle}%",
        ]
        try:
            from kivy.core.clipboard import Clipboard
            Clipboard.copy("\n".join(lines))
        except Exception:
            pass

    # ── Main update ───────────────────────────────────────────────────

    def update(self, state):
        # Armed state drives button enable/disable
        self._update_armed_state(state)

        # Armed indicator and mode display (always update)
        if state.armed:
            self.ids.armed_indicator.text = "ARMED"
            self.ids.armed_indicator.color = get_color("armed_color")
        else:
            self.ids.armed_indicator.text = "DISARMED"
            self.ids.armed_indicator.color = get_color("disarmed_color")
        self.ids.mode_display.text = f"Mode: {state.flight_mode}"

        # Status messages
        msgs = state.status_messages[-30:]
        lines = []
        for sm in reversed(msgs):
            import datetime
            ts = datetime.datetime.fromtimestamp(sm.timestamp).strftime(
                "%H:%M:%S")
            lines.append(f"[{ts}] [{sm.severity_name}] {sm.text}")
        self.ids.status_log.text = "\n".join(lines) if lines else "No messages"

        # Telemetry and HUD
        self._update_telemetry(state)
        self._update_hud(state)


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

    _PLOT_WINDOW = 30  # seconds of data to keep for plotting

    def toggle_pause(self):
        self._paused = not self._paused
        btn = self.ids.get('pause_btn')
        if btn:
            btn.text = 'Resume' if self._paused else 'Pause'

    def clear_plots(self):
        for pid in ('temp_plot', 'rh_plot'):
            p = self.ids.get(pid)
            if p:
                p.set_data({})

    def export_csv(self):
        app = App.get_running_app()
        s = app.vehicle_state
        if not s.h_time:
            return
        import csv
        import os
        if ON_ANDROID:
            base = os.path.join(_android_storage_base(), "exports")
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

        # Only plot the last _PLOT_WINDOW seconds of data
        t_latest = state.h_time[-1]
        t_cutoff = t_latest - self._PLOT_WINDOW

        # Find start index for the window
        start = 0
        for i, t in enumerate(state.h_time):
            if t >= t_cutoff:
                start = i
                break

        # Build temperature series from windowed history
        temp_series = {}
        for idx in range(3):
            name = f"T{idx + 1}"
            color = self._TEMP_COLORS[idx]
            pts = []
            for i in range(start, len(state.h_time)):
                t = state.h_time[i]
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
            for i in range(start, len(state.h_time)):
                t = state.h_time[i]
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


DEFAULT_THRESHOLDS = {
    "battery_pct_warn": 50,
    "battery_pct_crit": 30,
    "voltage_min": 22.0,
    "gps_sats_min": 6,
    "hdop_max": 3.0,
    "rssi_min": 40,
    "max_wind_speed": 15.0,
    "temp_min_c": -10.0,
    "temp_max_c": 50.0,
    "rh_min": 10.0,
    "rh_max": 95.0,
}

DEFAULT_WIND_COEFFS = {
    "ws_a": 37.1,
    "ws_b": 3.8,
}

DEFAULT_STREAM_RATE_HZ = 10


class SettingsScreen(Screen):
    """Alert thresholds, wind coefficients, and app settings with JSON persistence."""

    _THEME_MAP = {"Dark": "dark", "High Contrast": "high_contrast"}
    _THEME_DISPLAY = {v: k for k, v in _THEME_MAP.items()}

    _FIELDS = [
        ("battery_pct_warn", "th_batt_warn"),
        ("battery_pct_crit", "th_batt_crit"),
        ("voltage_min",      "th_volt_min"),
        ("gps_sats_min",     "th_gps_sats"),
        ("hdop_max",         "th_hdop_max"),
        ("rssi_min",         "th_rssi_min"),
        ("max_wind_speed",   "th_wind_max"),
        ("temp_min_c",       "th_temp_min"),
        ("temp_max_c",       "th_temp_max"),
        ("rh_min",           "th_rh_min"),
        ("rh_max",           "th_rh_max"),
    ]

    _WIND_FIELDS = [
        ("ws_a", "wind_ws_a"),
        ("ws_b", "wind_ws_b"),
    ]

    def on_enter(self):
        app = App.get_running_app()
        # Thresholds tab
        thresholds = app.settings_data.get("thresholds", {})
        for key, widget_id in self._FIELDS:
            val = thresholds.get(key, DEFAULT_THRESHOLDS[key])
            inp = self.ids.get(widget_id)
            if inp:
                inp.text = str(val)
        # Wind coefficients tab
        wind = app.settings_data.get("wind_coeffs", {})
        for key, widget_id in self._WIND_FIELDS:
            val = wind.get(key, DEFAULT_WIND_COEFFS[key])
            inp = self.ids.get(widget_id)
            if inp:
                inp.text = str(val)
        # Theme spinner
        spinner = self.ids.get("theme_spinner")
        if spinner:
            current = get_theme_name()
            spinner.text = self._THEME_DISPLAY.get(current, "Dark")
        # Stream rate
        rate_inp = self.ids.get("stream_rate_input")
        if rate_inp:
            rate_inp.text = str(
                app.settings_data.get("stream_rate_hz", DEFAULT_STREAM_RATE_HZ))

    # -- Alert Thresholds --

    def apply_thresholds(self):
        app = App.get_running_app()
        thresholds = {}
        for key, widget_id in self._FIELDS:
            inp = self.ids.get(widget_id)
            if inp:
                try:
                    thresholds[key] = float(inp.text)
                except ValueError:
                    thresholds[key] = DEFAULT_THRESHOLDS[key]
        app.settings_data["thresholds"] = thresholds
        _save_settings(app.settings_data)
        fb = self.ids.get('settings_feedback')
        if fb:
            fb.text = "Thresholds saved"

    def reset_defaults(self):
        app = App.get_running_app()
        app.settings_data["thresholds"] = dict(DEFAULT_THRESHOLDS)
        _save_settings(app.settings_data)
        for key, widget_id in self._FIELDS:
            inp = self.ids.get(widget_id)
            if inp:
                inp.text = str(DEFAULT_THRESHOLDS[key])
        fb = self.ids.get('settings_feedback')
        if fb:
            fb.text = "Reset to defaults"

    # -- Wind Coefficients --

    def apply_wind_coeffs(self):
        app = App.get_running_app()
        coeffs = {}
        for key, widget_id in self._WIND_FIELDS:
            inp = self.ids.get(widget_id)
            if inp:
                try:
                    coeffs[key] = float(inp.text)
                except ValueError:
                    coeffs[key] = DEFAULT_WIND_COEFFS[key]
        app.settings_data["wind_coeffs"] = coeffs
        _save_settings(app.settings_data)
        # Push new values to live mav_client and sim
        app.mav_client.ws_a = coeffs["ws_a"]
        app.mav_client.ws_b = coeffs["ws_b"]
        app.sim.ws_a = coeffs["ws_a"]
        app.sim.ws_b = coeffs["ws_b"]
        fb = self.ids.get('wind_feedback')
        if fb:
            fb.text = f"Saved: A={coeffs['ws_a']}, B={coeffs['ws_b']}"

    def reset_wind_defaults(self):
        app = App.get_running_app()
        app.settings_data["wind_coeffs"] = dict(DEFAULT_WIND_COEFFS)
        _save_settings(app.settings_data)
        for key, widget_id in self._WIND_FIELDS:
            inp = self.ids.get(widget_id)
            if inp:
                inp.text = str(DEFAULT_WIND_COEFFS[key])
        app.mav_client.ws_a = DEFAULT_WIND_COEFFS["ws_a"]
        app.mav_client.ws_b = DEFAULT_WIND_COEFFS["ws_b"]
        app.sim.ws_a = DEFAULT_WIND_COEFFS["ws_a"]
        app.sim.ws_b = DEFAULT_WIND_COEFFS["ws_b"]
        fb = self.ids.get('wind_feedback')
        if fb:
            fb.text = "Reset to defaults"

    # -- Theme --

    def on_theme_changed(self, display_name):
        theme_name = self._THEME_MAP.get(display_name, "dark")
        if theme_name == get_theme_name():
            return
        app = App.get_running_app()
        app.set_app_theme(theme_name)
        fb = self.ids.get("theme_feedback")
        if fb:
            fb.text = f"Theme: {display_name}"

    # -- Data Streams --

    def on_stream_rate_changed(self, text):
        try:
            rate = int(text)
        except ValueError:
            rate = DEFAULT_STREAM_RATE_HZ
        rate = max(1, min(10, rate))
        app = App.get_running_app()
        app.settings_data["stream_rate_hz"] = rate
        _save_settings(app.settings_data)
        # Update the input to show the clamped value
        inp = self.ids.get("stream_rate_input")
        if inp and inp.text != str(rate):
            inp.text = str(rate)
        fb = self.ids.get("stream_rate_feedback")
        if fb:
            fb.text = f"Stream rate: {rate} Hz (takes effect on next connection)"

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

    # ---- Theme color properties for KV binding ----
    theme_bg_root = ListProperty([0.12, 0.12, 0.14, 1])
    theme_bg_navbar = ListProperty([0.15, 0.15, 0.18, 1])
    theme_bg_input = ListProperty([0.2, 0.2, 0.25, 1])
    theme_bg_spinner = ListProperty([0.25, 0.25, 0.3, 1])
    theme_bg_status_log = ListProperty([0.08, 0.08, 0.1, 1])
    theme_text_primary = ListProperty([1, 1, 1, 1])
    theme_text_title = ListProperty([0.8, 0.85, 0.9, 1])
    theme_text_label = ListProperty([0.7, 0.7, 0.7, 1])
    theme_text_settings = ListProperty([0.65, 0.65, 0.7, 1])
    theme_text_tile_label = ListProperty([0.55, 0.6, 0.65, 1])
    theme_text_section = ListProperty([0.45, 0.48, 0.52, 1])
    theme_text_dim = ListProperty([0.4, 0.4, 0.4, 1])
    theme_text_detail = ListProperty([0.6, 0.6, 0.6, 1])
    theme_text_feedback = ListProperty([0.5, 0.7, 0.5, 1])
    theme_text_cmd_feedback = ListProperty([0.5, 0.6, 0.7, 1])
    theme_text_status_log = ListProperty([0.6, 0.7, 0.65, 1])
    theme_text_mode_display = ListProperty([0.6, 0.65, 0.7, 1])
    theme_text_copy_btn = ListProperty([0.85, 0.9, 0.95, 1])
    theme_text_last_update = ListProperty([0.5, 0.5, 0.5, 1])
    theme_text_formula = ListProperty([0.5, 0.55, 0.6, 1])
    theme_btn_connect = ListProperty([0.2, 0.55, 0.3, 1])
    theme_btn_action = ListProperty([0.25, 0.35, 0.5, 1])
    theme_btn_danger = ListProperty([0.7, 0.3, 0.15, 1])
    theme_btn_safe = ListProperty([0.2, 0.45, 0.25, 1])
    theme_btn_warning = ListProperty([0.55, 0.35, 0.1, 1])
    theme_btn_clear = ListProperty([0.5, 0.25, 0.2, 1])
    theme_btn_generate = ListProperty([0.25, 0.45, 0.55, 1])
    theme_btn_apply = ListProperty([0.2, 0.5, 0.3, 1])
    theme_btn_reset = ListProperty([0.5, 0.25, 0.2, 1])
    theme_btn_map = ListProperty([0.2, 0.3, 0.4, 1])

    def apply_theme(self):
        """Push all theme colors from current theme dict into ListProperties."""
        self.theme_bg_root = list(get_color("bg_root"))
        self.theme_bg_navbar = list(get_color("bg_navbar"))
        self.theme_bg_input = list(get_color("bg_input"))
        self.theme_bg_spinner = list(get_color("bg_spinner"))
        self.theme_bg_status_log = list(get_color("bg_status_log"))
        self.theme_text_primary = list(get_color("text_primary"))
        self.theme_text_title = list(get_color("text_title"))
        self.theme_text_label = list(get_color("text_label"))
        self.theme_text_settings = list(get_color("text_settings"))
        self.theme_text_tile_label = list(get_color("text_tile_label"))
        self.theme_text_section = list(get_color("text_section"))
        self.theme_text_dim = list(get_color("text_dim"))
        self.theme_text_detail = list(get_color("text_detail"))
        self.theme_text_feedback = list(get_color("text_feedback"))
        self.theme_text_cmd_feedback = list(get_color("text_cmd_feedback"))
        self.theme_text_status_log = list(get_color("text_status_log"))
        self.theme_text_mode_display = list(get_color("text_mode_display"))
        self.theme_text_copy_btn = list(get_color("text_copy_btn"))
        self.theme_text_last_update = list(get_color("text_last_update"))
        self.theme_text_formula = list(get_color("text_formula"))
        self.theme_btn_connect = list(get_color("btn_connect"))
        self.theme_btn_action = list(get_color("btn_action"))
        self.theme_btn_danger = list(get_color("btn_danger"))
        self.theme_btn_safe = list(get_color("btn_safe"))
        self.theme_btn_warning = list(get_color("btn_warning"))
        self.theme_btn_clear = list(get_color("btn_clear"))
        self.theme_btn_generate = list(get_color("btn_generate"))
        self.theme_btn_apply = list(get_color("btn_apply"))
        self.theme_btn_reset = list(get_color("btn_reset"))
        self.theme_btn_map = list(get_color("btn_map"))

    def set_app_theme(self, name):
        """Switch theme, persist choice, and refresh UI."""
        set_theme(name)
        self.settings_data["theme"] = name
        _save_settings(self.settings_data)
        self.apply_theme()

    def build(self):
        self.settings_data = _load_settings()

        # Apply persisted theme
        theme_name = self.settings_data.get("theme", "dark")
        set_theme(theme_name)
        self.apply_theme()

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

        # Apply persisted wind coefficients
        wind = self.settings_data.get("wind_coeffs", {})
        self.mav_client.ws_a = wind.get("ws_a", DEFAULT_WIND_COEFFS["ws_a"])
        self.mav_client.ws_b = wind.get("ws_b", DEFAULT_WIND_COEFFS["ws_b"])
        self.sim.ws_a = wind.get("ws_a", DEFAULT_WIND_COEFFS["ws_a"])
        self.sim.ws_b = wind.get("ws_b", DEFAULT_WIND_COEFFS["ws_b"])

        # Apply persisted stream rate
        self.mav_client.stream_rate_hz = self.settings_data.get(
            "stream_rate_hz", DEFAULT_STREAM_RATE_HZ)

        root = GCSRoot()
        return root

    def on_start(self):
        """Called after build — the widget tree from KV is ready."""
        sm = self.root.ids.sm
        sm.transition = SlideTransition(duration=0.2)
        sm.add_widget(ConnectionScreen(name="connection"))
        sm.add_widget(FlightScreen(name="flight"))
        sm.add_widget(SensorPlotScreen(name="sensor_plots"))
        sm.add_widget(ProfileScreen(name="profile"))
        sm.add_widget(MapScreen(name="map"))
        sm.add_widget(SettingsScreen(name="settings"))
        self.sm = sm

        # Request storage permissions on Android (one frame after on_start)
        if ON_ANDROID:
            Clock.schedule_once(self._request_android_permissions, 0)

    def _request_android_permissions(self, dt):
        """Request runtime storage permissions on Android 6+."""
        try:
            from android.permissions import (  # type: ignore
                request_permissions, check_permission, Permission,
            )
            if check_permission(Permission.WRITE_EXTERNAL_STORAGE):
                log.info("Storage permission already granted")
                self._on_storage_ready()
            else:
                log.info("Requesting storage permissions…")
                request_permissions(
                    [Permission.WRITE_EXTERNAL_STORAGE,
                     Permission.READ_EXTERNAL_STORAGE],
                    callback=self._permission_callback,
                )
        except Exception:
            log.exception("Failed to request Android permissions")

    def _permission_callback(self, permissions, grant_results):
        """Called asynchronously after the user responds to the permission dialog."""
        if all(grant_results):
            log.info("Storage permissions granted")
            Clock.schedule_once(lambda dt: self._on_storage_ready(), 0)
        else:
            log.warning("Storage permissions denied — using app-private storage")

    def _on_storage_ready(self):
        """Create the dedicated app folder tree on internal storage."""
        base = _android_storage_base()
        for sub in ("logs", "exports", "settings"):
            try:
                os.makedirs(os.path.join(base, sub), exist_ok=True)
            except Exception:
                log.exception("Failed to create %s/%s", base, sub)
        log.info("App storage folder ready: %s", base)

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
