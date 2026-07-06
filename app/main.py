"""
CopterSonde Ground Control Station – Kivy application entry point.

Multi-screen GCS app with bottom navigation bar.
"""

import datetime
import json
import math
import os
import sys
import time

# ---------------------------------------------------------------------------
# Ensure the repo root is on sys.path so `gcs.*` and `app.*` imports work
# regardless of how the app is launched (CLI, IDE, PyInstaller, Buildozer).
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ---------------------------------------------------------------------------
# PyInstaller --windowed: redirect stdio so Kivy's console logger doesn't
# recurse when sys.stderr is None (frozen builds have no console).
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False) and sys.stderr is None:
    sys.stderr = open(os.devnull, "w")
if getattr(sys, "frozen", False) and sys.stdout is None:
    sys.stdout = open(os.devnull, "w")

# ---------------------------------------------------------------------------
# Kivy configuration – must come BEFORE any other kivy import
# ---------------------------------------------------------------------------
from kivy.config import Config  # noqa: E402

Config.set("graphics", "width", "960")
Config.set("graphics", "height", "540")
Config.set("graphics", "resizable", "1")

from kivy.app import App  # noqa: E402
from kivy.clock import Clock  # noqa: E402
from kivy.metrics import dp  # noqa: E402
from kivy.lang import Builder  # noqa: E402
from kivy.uix.boxlayout import BoxLayout  # noqa: E402
from kivy.uix.tabbedpanel import TabbedPanel, TabbedPanelItem  # noqa: E402,F401
from kivy.uix.screenmanager import ScreenManager, Screen, SlideTransition  # noqa: E402
from kivy.properties import StringProperty, ListProperty, BooleanProperty  # noqa: E402

from gcs.logutil import setup_logging, attach_file_handler, get_logger  # noqa: E402
from gcs.storage_paths import mirror_file, output_dirs, program_data_dir  # noqa: E402
from gcs.event_bus import EventBus, EventType  # noqa: E402
from gcs.vehicle_state import VehicleState  # noqa: E402
from gcs.mavlink_client import MAVLinkClient  # noqa: E402
from gcs.sim_telemetry import SimTelemetry  # noqa: E402
from gcs.tlog_replay import TlogReplayClient  # noqa: E402
from app.hud_widget import FlightHUD  # noqa: E402,F401
from app.plot_widget import TimeSeriesPlot, ProfilePlot  # noqa: E402,F401
from app.map_widget import MapWidget  # noqa: E402,F401
from app.tlog_picker import open_tlog_picker  # noqa: E402
from app.device_location import DeviceLocation  # noqa: E402
from app.theme import get_color, set_theme, get_theme_name, THEME_NAMES  # noqa: E402

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
# On Android (Buildozer), the `android` module is available.  Android uses
# udpout:127.0.0.1:14552 because HereLink routes MAVLink to the app via
# a local UDP socket.  Desktop uses udpin:0.0.0.0:14550 to listen for
# incoming MAVLink connections on all interfaces.
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
# "Custom" is a special sentinel: empty fields signal the UI to show
# editable input fields for manual connection configuration.
# "Replay Log File" is the same kind of sentinel: it reveals the replay
# file-selection row instead of the custom transport fields.
CONNECTION_PRESETS = [
    ("HereLink Radio",      "udpout",  "127.0.0.1", "14552"),
    ("HereLink Hotspot",    "udp",  "127.0.0.1", "14550"),
    ("SITL (mav-disabled)",  "tcp",    "127.0.0.1", "5760"),
    ("SITL (mav-enabled)",  "udp",  "127.0.0.1", "14560"),
    ("Replay Log File", "", "", ""),
    ("Custom", "", "", ""),
]
PRESET_NAMES = [p[0] for p in CONNECTION_PRESETS]
PRESET_MAP = {p[0]: p[1:] for p in CONNECTION_PRESETS}

UI_UPDATE_HZ = 10

setup_logging()
attach_file_handler()
log = get_logger("app")

# ---------------------------------------------------------------------------
# Settings persistence (JSON file)
# ---------------------------------------------------------------------------
# All user preferences (connection preset, thresholds, wind coefficients,
# theme choice, stream rate, Remote ID identity) are persisted in a single
# JSON file in [program data]/Settings (SoW 205195 #27): a location the user
# is not expected to visit but that is not protected from access (#2) —
# %LOCALAPPDATA% on Windows, built-in storage's app dir on Android.

def _settings_path():
    return os.path.join(program_data_dir("Settings"), "settings.json")


def _load_settings():
    p = _settings_path()
    if os.path.exists(p):
        try:
            with open(p, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}  # empty dict means all defaults will be used


def _save_settings(data):
    p = _settings_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.error("Failed to save settings to %s: %s", p, e)


# KV file path — loaded after all Screen class definitions so the KV
# parser can resolve class names.  PyInstaller bundles data files under
# sys._MEIPASS, so we check for frozen mode.
if getattr(sys, "frozen", False):
    _KV_PATH = os.path.join(sys._MEIPASS, "app", "app.kv")
else:
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
    """Connection management: transport selection, connect/disconnect, demo mode.

    Uses a preset spinner for common configurations, a "Custom" mode
    that reveals editable fields for manual connection setup, and a
    "Replay Log File" mode that reveals a telemetry-log file selector.
    """

    def on_enter(self):
        # Restore last-used settings into UI widgets
        app = App.get_running_app()
        settings = app.settings_data
        # Restore custom fields
        self.ids.conn_type_spinner.text = settings.get("last_conn_type", DEFAULT_CONN_TYPE)
        self.ids.ip_input.text = settings.get("last_ip", DEFAULT_IP)
        self.ids.port_input.text = str(settings.get("last_port", DEFAULT_PORT))
        # Restore preset selection
        preset_spinner = self.ids.get("preset_spinner")
        if preset_spinner and preset_spinner.text == "":
            preset_spinner.text = settings.get(
                "last_preset",
                "HereLink Radio",
            )

    def on_preset_changed(self, preset_name):
        """Show/hide the per-preset config rows based on selection.

        "Custom" reveals the editable conn_type/ip/port fields;
        "Replay Log File" reveals the replay file selector.  Any other
        preset collapses both rows to zero height.
        """
        box = self.ids.get("custom_conn_box")
        if box:
            if preset_name == "Custom":
                box.height = dp(44)
                box.opacity = 1
            else:
                box.height = 0
                box.opacity = 0
        replay_box = self.ids.get("replay_conn_box")
        if replay_box:
            if preset_name == "Replay Log File":
                replay_box.height = dp(44)
                replay_box.opacity = 1
            else:
                replay_box.height = 0
                replay_box.opacity = 0
        # With nothing active, the connect button doubles as the replay
        # starter — relabel it to match the selected preset.  While any
        # session (live/demo/replay) is running the button text belongs
        # to that session's stop action, so leave it alone.
        btn = self.ids.get("connect_btn")
        if btn and not self._session_active():
            btn.text = self._idle_connect_btn_text(preset_name)

    def _session_active(self):
        """True while a live connection, demo, or replay is running."""
        app = App.get_running_app()
        return (app.mav_client.running or app.sim.running
                or app.replay_client.running)

    def _idle_connect_btn_text(self, preset_name=None):
        """Connect-button label when no session is active."""
        if preset_name is None:
            preset_name = self.ids.preset_spinner.text
        return ("Start Replay" if preset_name == "Replay Log File"
                else "Connect")

    # ── Replay file selection ─────────────────────────────────────────
    # The picker popup (app.tlog_picker) lists recorded *.tlog files;
    # the chosen absolute path is stored on the screen and its basename
    # is shown in the replay row's label.

    _replay_filepath = None

    def on_choose_replay_file(self):
        open_tlog_picker(self._on_replay_file_selected)

    def _on_replay_file_selected(self, filepath):
        self._replay_filepath = filepath
        lbl = self.ids.get("replay_file_label")
        if lbl:
            lbl.text = os.path.basename(filepath)

    # ── Replay start / stop ───────────────────────────────────────────
    # Replay is mutually exclusive with the live connection and demo
    # mode (all three drive the same shared VehicleState), so starting
    # is refused while either is active.  Stopping is immediate — the
    # hold-to-disconnect guard exists to protect a flying vehicle, and
    # a replay is not one.

    def _start_replay(self, app):
        """Validate the selected file and start the replay client."""
        if app.mav_client.running or app.sim.running:
            self.ids.detail_label.text = (
                "Disconnect or stop demo mode before starting a replay")
            return

        path = self._replay_filepath
        if not path:
            self.ids.detail_label.text = (
                "No file selected — choose a .tlog file first")
            return
        if not os.path.isfile(path):
            self.ids.detail_label.text = (
                f"File not found: {os.path.basename(path)}")
            return
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            self.ids.detail_label.text = f"Cannot read file: {exc}"
            return
        if size == 0:
            self.ids.detail_label.text = (
                f"File is empty: {os.path.basename(path)}")
            return

        sd = app.settings_data
        d = DEFAULT_REPLAY_OUTPUTS
        generate_logs = bool(
            sd.get("replay_generate_debug_log", d["replay_generate_debug_log"]))

        self._set_status(
            "Starting Replay…", get_color("status_warn"),
            f"Loading {os.path.basename(path)}…")

        try:
            app.replay_client.start(
                path,
                generate_logs,
                emit_raw=bool(sd.get("replay_generate_raw", d["replay_generate_raw"])),
                emit_alm=bool(sd.get("replay_generate_alm", d["replay_generate_alm"])),
                emit_tim=bool(sd.get("replay_generate_tim", d["replay_generate_tim"])),
                emit_wmo=bool(sd.get("replay_generate_wmo", d["replay_generate_wmo"])),
            )
        except Exception as exc:
            log.error("Replay failed to start: %s", exc)
            self._set_status("Replay Error", get_color("status_conn_err"),
                             str(exc))
            return

        self.ids.connect_btn.text = "Stop Replay"
        self.ids.connect_btn.background_color = list(get_color("btn_disconnect"))
        self.ids.demo_toggle.disabled = True
        self._start_ui_refresh(app)

    def _stop_replay(self, app):
        """Stop the replay immediately and restore the idle UI."""
        app.replay_client.stop()
        self._stop_ui_refresh(app)
        self.ids.connect_btn.text = self._idle_connect_btn_text()
        self.ids.connect_btn.background_color = list(get_color("btn_connect"))
        self.ids.demo_toggle.disabled = False
        self._set_status("Not Connected", get_color("status_error"),
                         "Disconnected")

    # ── Hold-to-disconnect safety pattern ─────────────────────────────
    # Prevents accidental disconnects: user must press and hold the
    # button for 1 second, then confirm in a popup.  Releasing early
    # cancels the action.  This two-stage guard is critical because
    # disconnecting mid-flight could lose vehicle telemetry.

    _hold_event = None

    def on_connect_press(self):
        app = App.get_running_app()
        if app.replay_client.running:
            # Replay stops immediately on release — never arm the
            # hold-to-disconnect timer (that guard protects a flying
            # vehicle; a replay is not one).
            return
        if app.mav_client.running:
            # Start 1-second hold timer for disconnect
            self.ids.connect_btn.text = "Hold to disconnect…"
            self._hold_event = Clock.schedule_once(
                lambda dt: self._on_hold_complete(), 1.0)
        # For connect / demo-stop / replay, action happens on release

    def on_connect_release(self):
        app = App.get_running_app()
        if app.replay_client.running:
            # Routed before the hold logic so the live path's safety
            # machinery is never touched by a replay session.
            self._stop_replay(app)
            return
        if self._hold_event is not None:
            # Released before 1s — cancel the disconnect attempt
            self._hold_event.cancel()
            self._hold_event = None
            if app.mav_client.running:
                self.ids.connect_btn.text = "Disconnect (hold 1s)"
            return
        # Normal release actions (connect, start replay, or stop demo)
        if app.sim.running:
            self._disconnect(app)
        elif not app.mav_client.running:
            if self.ids.preset_spinner.text == "Replay Log File":
                self._start_replay(app)
            else:
                self._connect(app)

    def _on_hold_complete(self):
        self._hold_event = None
        app = App.get_running_app()
        if app.mav_client.running:
            self._confirm_disconnect(app)

    def _confirm_disconnect(self, app):
        from kivy.uix.popup import Popup
        from kivy.uix.label import Label
        from kivy.uix.button import Button

        content = BoxLayout(orientation='vertical', padding=10, spacing=10)
        content.add_widget(Label(
            text='Are you sure you want to disconnect\nfrom the vehicle?',
            font_size='14sp', halign='center',
            color=get_color("text_label")))

        btn_row = BoxLayout(size_hint_y=None, height=44, spacing=10)
        popup = Popup(title='Confirm Disconnect', content=content,
                      size_hint=(0.6, 0.35), auto_dismiss=False)

        yes_btn = Button(text='Disconnect',
                         background_color=list(get_color("btn_disconnect")))
        no_btn = Button(text='Cancel',
                        background_color=list(get_color("btn_clear")))

        yes_btn.bind(on_release=lambda *_: (popup.dismiss(), self._disconnect(app)))

        def _on_cancel(*_):
            popup.dismiss()
            self.ids.connect_btn.text = "Disconnect (hold 1s)"
        no_btn.bind(on_release=_on_cancel)

        btn_row.add_widget(yes_btn)
        btn_row.add_widget(no_btn)
        content.add_widget(btn_row)
        popup.open()

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
            self.ids.connect_btn.text = self._idle_connect_btn_text()
            self.ids.connect_btn.background_color = list(get_color("btn_connect"))
            self._stop_ui_refresh(app)
            self._set_status("Not Connected", get_color("status_error"), "Disconnected")

    def _connect(self, app):
        """Resolve connection parameters and start the MAVLink client."""
        preset_name = self.ids.preset_spinner.text
        preset = PRESET_MAP.get(preset_name)

        if preset and preset[0]:
            # Named preset — use its predefined values
            conn_type, ip, port = preset
        else:
            # Custom mode — read user-entered values from input fields
            conn_type = self.ids.conn_type_spinner.text or DEFAULT_CONN_TYPE
            ip = self.ids.ip_input.text.strip() or DEFAULT_IP
            port = self.ids.port_input.text.strip() or str(DEFAULT_PORT)

        # Persist connection settings so they restore on next launch
        app.settings_data["last_preset"] = preset_name
        app.settings_data["last_conn_type"] = conn_type
        app.settings_data["last_ip"] = ip
        app.settings_data["last_port"] = int(port)
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

        self.ids.connect_btn.text = "Disconnect (hold 1s)"
        self.ids.connect_btn.background_color = list(get_color("btn_disconnect"))
        self.ids.demo_toggle.disabled = True
        self._start_ui_refresh(app)

    def _disconnect(self, app):
        app.mav_client.stop()
        app.sim.stop()
        self._stop_ui_refresh(app)
        self.ids.connect_btn.text = self._idle_connect_btn_text()
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
        # Replay session — routed before the heartbeat health checks,
        # which describe a live link and would mislabel a paced replay
        # (e.g. "No Heartbeat" once the frozen end-of-file state ages
        # out).  After EOF the engine freezes the final state and the
        # button stays "Stop Replay" until pressed — by design.
        if app.replay_client.running:
            rc = app.replay_client
            if rc.finished:
                self._set_status(
                    "Replay complete", get_color("status_warn"),
                    rc.filename)
            else:
                self._set_status(
                    "Replaying", get_color("status_healthy"),
                    f"{rc.filename} — {rc.progress:.0f}%")
            return
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
# All items must be checked before the ARM button is enabled.
# This forces the operator to manually verify each safety condition.

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

    # Remote ID readiness indicator (SoW 205195 #38), bound from KV.
    rid_text = StringProperty("Remote ID")
    rid_color = ListProperty([0.3, 0.3, 0.3, 1])

    # MAVLink STATUSTEXT severity -> hex color for the status log
    _SEV_COLORS = {
        0: "ff5252",   # EMERGENCY  - red
        1: "ff5252",   # ALERT      - red
        2: "ff5252",   # CRITICAL   - red
        3: "ffa726",   # ERROR      - orange
        4: "ffa726",   # WARNING    - orange
        5: "4fc3f7",   # NOTICE     - blue
        6: "4fc3f7",   # INFO       - blue
        7: "4fc3f7",   # DEBUG      - blue
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Pre-flight checklist gating — ARM button stays disabled until
        # all checklist items are checked and the user clicks Proceed.
        self._checklist_complete = False
        self._checklist_popup = None
        self._proceed_btn = None
        self._check_states = {}
        # Flight timer: starts on armed, stops on disarmed.
        # Tracks state transitions to avoid repeated start/stop.
        self._prev_armed = None
        self._flight_timer_start = None   # monotonic() timestamp when armed
        self._flight_timer_elapsed = 0.0  # accumulated seconds (survives pause)
        # Status message caching — only rebuild the markup string when
        # new messages arrive, not every UI tick.
        self._cached_status_len = 0
        self._cached_status_text = "No messages"

    # ── Remote ID indicator (SoW 205195 #38) ────────────────────

    def on_enter(self):
        # Refresh once on entry so the indicator is correct even before a
        # connection starts the periodic update loop.
        self._update_rid_indicator()

    def _update_rid_indicator(self):
        """Green: identity set and device GPS fix.  Red: fixable problem
        (identity missing, or no fix yet on a GPS-capable device).
        Yellow: this platform has no GPS, so green is unreachable — a
        deliberate third state beyond SoW #38 for desktop test runs."""
        app = App.get_running_app()
        ids_ok = bool(app.mav_client.operator_id) and bool(
            app.mav_client.drone_serial)
        if not ids_ok:
            text, color = "Remote ID: ID NOT SET", "tile_red"
        elif app.device_location.has_recent_fix():
            text, color = "Remote ID: OK", "tile_green"
        elif not ON_ANDROID:
            text, color = "Remote ID: NO GPS ON PC", "tile_yellow"
        else:
            text, color = "Remote ID: NO GPS FIX", "tile_red"
        self.rid_text = text
        self.rid_color = list(_tile_color(color))

    # ── Telemetry update ──────────────────────────────────────────────

    def _update_telemetry(self, state):
        if not state.is_healthy():
            return

        # Set non-threshold tiles to theme default so they update on
        # theme change (e.g. high-contrast needs a light background).
        default = _tile_color("tile_default")
        for tid in ("tile_mode", "tile_time", "tile_voltage", "tile_current",
                     "tile_alt_rel", "tile_alt_amsl", "tile_heading",
                     "tile_gndspd", "tile_vertspd", "tile_throttle"):
            self.ids[tid].tile_color = default

        # System
        self.ids.tile_mode.value_text = state.flight_mode
        self.ids.tile_armed.value_text = "ARMED" if state.armed else "DISARMED"
        self.ids.tile_armed.tile_color = (
            _tile_color("tile_green") if state.armed else _tile_color("tile_red")
        )

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

    def _in_replay(self):
        """True while a replay session is running.

        SoW 205195 #35: the replay datastream is read-only, so command
        buttons must simply do nothing — no popup, no feedback, no
        transmission attempt against the (disconnected) live client.
        """
        return App.get_running_app().replay_client.running

    def on_generate_mission(self):
        if self._in_replay():
            return  # SoW #35
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
        if self._in_replay():
            return  # SoW #35
        # Gate: ARM is only allowed after the pre-flight checklist is complete
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
        if self._in_replay():
            return  # SoW #35
        self._confirm("Loiter", "Switch to LOITER mode?",
                      lambda: self._do_set_mode("LOITER"))

    # ── Command: RTL ──────────────────────────────────────────────────

    def on_rtl(self):
        if self._in_replay():
            return  # SoW #35
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
        if self._in_replay():
            return  # SoW #35: the armed gate below reads replayed state
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

    # ── Armed state transition management ────────────────────────────
    # Detects DISARMED->ARMED and ARMED->DISARMED transitions to:
    #   - Start/stop the flight timer
    #   - Enable/disable checklist and ARM buttons
    #   - Auto-dismiss checklist popup if still open when armed

    def _update_armed_state(self, state):
        armed = state.armed
        if armed == self._prev_armed:
            return  # no transition — skip

        if armed:
            # DISARMED -> ARMED: start flight timer from zero
            self._flight_timer_start = time.monotonic()
            self._flight_timer_elapsed = 0.0
            # Lock out checklist and arm buttons while flying
            self.ids.checklist_btn.disabled = True
            self.ids.arm_btn.disabled = True
            if self._checklist_popup:
                self._checklist_popup.dismiss()
                self._checklist_popup = None
                self._proceed_btn = None
        else:
            # ARMED -> DISARMED: accumulate flight time and stop timer
            if self._flight_timer_start is not None:
                self._flight_timer_elapsed += (
                    time.monotonic() - self._flight_timer_start)
                self._flight_timer_start = None
            # Re-enable checklist; require re-completion before next ARM
            self.ids.checklist_btn.disabled = False
            self._checklist_complete = False
            self.ids.arm_btn.disabled = True

        self._prev_armed = armed

    # ── Main update ───────────────────────────────────────────────────

    def update(self, state):
        # Remote ID readiness — independent of vehicle link health
        self._update_rid_indicator()

        # Armed state drives button enable/disable
        self._update_armed_state(state)

        # Flight timer — always updated regardless of link health so the
        # displayed time never freezes during brief communication dropouts.
        elapsed = self._flight_timer_elapsed
        if self._flight_timer_start is not None:
            elapsed += time.monotonic() - self._flight_timer_start
        t = int(elapsed)
        m, s = divmod(t, 60)
        h, m = divmod(m, 60)
        self.ids.tile_time.value_text = f"{h:02d}:{m:02d}:{s:02d}"

        # Armed indicator and mode display (always update)
        if state.armed:
            self.ids.armed_indicator.text = "ARMED"
            self.ids.armed_indicator.color = get_color("armed_color")
        else:
            self.ids.armed_indicator.text = "DISARMED"
            self.ids.armed_indicator.color = get_color("disarmed_color")
        self.ids.mode_display.text = f"Mode: {state.flight_mode}"

        # Status message caching: only rebuild the Kivy markup string
        # when new messages arrive (cheap len() check vs expensive string ops).
        msg_count = len(state.status_messages)
        if msg_count != self._cached_status_len:
            self._cached_status_len = msg_count
            msgs = state.status_messages[-30:]  # show last 30 messages
            lines = []
            for sm in reversed(msgs):  # newest first
                ts = datetime.datetime.fromtimestamp(sm.timestamp).strftime(
                    "%H:%M:%S")
                hex_col = self._SEV_COLORS.get(sm.severity, "4fc3f7")
                # Escape Kivy markup special chars to prevent rendering errors
                safe_text = sm.text.replace("&", "&amp;").replace(
                    "[", "&bl;").replace("]", "&br;")
                lines.append(
                    f"[color={hex_col}]&bl;{ts}&br; "
                    f"&bl;{sm.severity_name}&br; {safe_text}[/color]"
                )
            self._cached_status_text = "\n".join(lines) if lines else "No messages"
        self.ids.status_log.text = self._cached_status_text

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
        fb = self.ids.get('export_feedback')
        if not s.h_time:
            if fb:
                fb.text = "No data to export"
            return
        import csv
        import os
        import datetime as _dt
        # [usr access intended]/exports (SoW #1); mirrored per #3 when the
        # primary is the SD card.
        base, backup_dir = output_dirs("exports")
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(base, f"sensors_{ts}.csv")
        try:
            os.makedirs(base, exist_ok=True)
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
            mirror_file(path, backup_dir)
            if fb:
                fb.text = f"Saved: {path}"
        except Exception as e:
            log.error("CSV export failed: %s", e)
            if fb:
                fb.text = f"Export failed: {e}"

    def update(self, state):
        if self._paused:
            return
        if not state.h_time:
            return

        # Snapshot the deques to avoid "deque mutated during iteration"
        # when the background IO/sim thread appends concurrently.
        h_time = list(state.h_time)
        h_temp_sensors = list(state.h_temp_sensors)
        h_rh_sensors = list(state.h_rh_sensors)
        n = len(h_time)

        # ── Rolling time window ──────────────────────────────────────
        # Only plot the last _PLOT_WINDOW seconds.  Use binary search to
        # find the start index in O(log n) instead of scanning O(n).
        t_latest = h_time[-1]
        t_cutoff = t_latest - self._PLOT_WINDOW

        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            if h_time[mid] < t_cutoff:
                lo = mid + 1
            else:
                hi = mid
        start = lo  # first index within the time window

        # Build temperature series from windowed history
        temp_series = {}
        for idx in range(3):
            name = f"T{idx + 1}"
            color = self._TEMP_COLORS[idx]
            pts = []
            for i in range(start, n):
                t = h_time[i]
                sensors = h_temp_sensors[i] if i < len(h_temp_sensors) else []
                if idx < len(sensors) and math.isfinite(sensors[idx]) and sensors[idx] > 0:
                    # Convert from Kelvin (MAVLink) to Celsius for display
                    pts.append((t, sensors[idx] - 273.15))
            temp_series[name] = (color, pts)

        # Build RH series (already in percent, no conversion needed)
        rh_series = {}
        for idx in range(3):
            name = f"RH{idx + 1}"
            color = self._RH_COLORS[idx]
            pts = []
            for i in range(start, n):
                t = h_time[i]
                sensors = h_rh_sensors[i] if i < len(h_rh_sensors) else []
                if idx < len(sensors) and math.isfinite(sensors[idx]) and sensors[idx] > 0:
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

    _MAX_PROFILE_POINTS = 300  # downsample limit for mobile performance

    def clear_profile(self):
        app = App.get_running_app()
        app.vehicle_state.clear_history()
        for pid in ('temp_profile', 'wind_profile'):
            p = self.ids.get(pid)
            if p:
                p.set_data({})

    @staticmethod
    def _downsample(pts):
        """Uniform stride downsample, always keeping the last point."""
        n = len(pts)
        if n <= ProfileScreen._MAX_PROFILE_POINTS:
            return pts
        stride = n // ProfileScreen._MAX_PROFILE_POINTS
        out = pts[::stride]
        if out[-1] is not pts[-1]:
            out.append(pts[-1])
        return out

    def update(self, state):
        if not state.h_time:
            return

        # Snapshot deques to avoid "deque mutated during iteration"
        # when the background IO/sim thread appends concurrently.
        h_alt_rel = list(state.h_alt_rel)
        h_temperature = list(state.h_temperature)
        h_dew_temp = list(state.h_dew_temp)
        h_wind_speed = list(state.h_wind_speed)

        _isfinite = math.isfinite  # local ref avoids repeated attr lookup

        def _valid(v):
            """Return True if v is a finite number (not NaN/inf/None)."""
            try:
                return _isfinite(float(v))
            except (TypeError, ValueError):
                return False

        # Temperature & Dew Point vs Altitude
        temp_pts, dew_pts = [], []
        for i, alt in enumerate(h_alt_rel):
            if not _valid(alt):
                continue
            if i < len(h_temperature) and _valid(h_temperature[i]):
                temp_pts.append((h_temperature[i], alt))
            if i < len(h_dew_temp) and _valid(h_dew_temp[i]):
                dew_pts.append((h_dew_temp[i], alt))

        temp_profile = self.ids.get('temp_profile')
        if temp_profile:
            temp_profile.set_data({
                'Temp': ((0.9, 0.3, 0.3, 1), self._downsample(temp_pts)),
                'Dew':  ((0.3, 0.7, 0.95, 1), self._downsample(dew_pts)),
            })

        # Wind Speed vs Altitude
        wspd_pts = []
        for i, alt in enumerate(h_alt_rel):
            if not _valid(alt):
                continue
            if i < len(h_wind_speed) and _valid(h_wind_speed[i]):
                wspd_pts.append((h_wind_speed[i], alt))

        wind_profile = self.ids.get('wind_profile')
        if wind_profile:
            wind_profile.set_data({
                'Wind Spd': ((0.3, 0.85, 0.5, 1), self._downsample(wspd_pts)),
            })


class MapScreen(Screen):
    """Satellite map with drone position, track, and ADS-B targets."""

    def on_toggle_track(self):
        """Toggle track visibility and update button color."""
        m = self.ids.get('map_view')
        btn = self.ids.get('track_btn')
        if m:
            is_on = m.toggle_track()
            if btn:
                app = App.get_running_app()
                btn.background_color = (
                    app.theme_btn_toggle_on if is_on
                    else app.theme_btn_toggle_off)

    def on_toggle_adsb(self):
        """Toggle ADS-B overlay and update button color."""
        m = self.ids.get('map_view')
        btn = self.ids.get('adsb_btn')
        if m:
            is_on = m.toggle_adsb()
            if btn:
                app = App.get_running_app()
                btn.background_color = (
                    app.theme_btn_toggle_on if is_on
                    else app.theme_btn_toggle_off)

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


# Alert thresholds — used to color-code telemetry tiles (green/yellow/red)
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

# Wind speed calibration coefficients for the CopterSonde anemometer
DEFAULT_WIND_COEFFS = {
    "ws_a": 37.1,
    "ws_b": 3.8,
}

DEFAULT_STREAM_RATE_HZ = 10

# Per-message replay-output toggles (settings keys -> default).  These gate
# which files a *replay* writes; they never affect a live connection.  The four
# message outputs default ON (a replay reproduces the full message set, matching
# the agreed behavior); the Debug MAVLink dump defaults OFF as a diagnostic.
DEFAULT_REPLAY_OUTPUTS = {
    "replay_generate_debug_log": False,
    "replay_generate_raw": True,
    "replay_generate_alm": True,
    "replay_generate_tim": True,
    "replay_generate_wmo": True,
}

# Operator identity for Remote ID and the message outputs.  Operator-entered
# strings with no meaningful default until the operator fills them in.
ODID_DEFAULTS = {
    "operator_id": "",
    "drone_serial": "",
}

# Characters barred from the operator identity fields: path separators plus the
# Windows/FAT-illegal set (the Android SD-card target is the strictest), the CSV
# delimiter (these values appear in comma-delimited files too), and control
# characters.  One rule keeps a value safe in both filenames and CSV fields.
_ID_ILLEGAL = set('/\\:*?"<>|,') | {chr(c) for c in range(32)} | {chr(127)}


def _sanitize_id(text):
    """Drop characters unsafe for a filename or a CSV field (see _ID_ILLEGAL)."""
    return "".join(ch for ch in text if ch not in _ID_ILLEGAL)


class SettingsScreen(Screen):
    """Alert thresholds, wind coefficients, and app settings with JSON persistence.

    All settings are persisted immediately on change via _save_settings()
    so they survive app restarts.
    """

    # Maps between UI display names and internal theme identifiers
    _THEME_MAP = {"Dark": "dark", "High Contrast": "high_contrast"}
    _THEME_DISPLAY = {v: k for k, v in _THEME_MAP.items()}

    # Mapping: (settings_data key, KV widget id) for threshold inputs.
    # Used to generically load/save all threshold fields in loops.
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

    _ODID_FIELDS = [
        ("operator_id",  "odid_operator_id"),
        ("drone_serial", "odid_drone_serial"),
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
        # Remote ID tab
        odid = app.settings_data.get("odid", {})
        for key, widget_id in self._ODID_FIELDS:
            val = odid.get(key, ODID_DEFAULTS[key])
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
        # Replay-output toggles (replay only; never affect a live connection)
        for key, widget_id in self._REPLAY_SWITCHES:
            sw = self.ids.get(widget_id)
            if sw:
                sw.active = bool(
                    app.settings_data.get(key, DEFAULT_REPLAY_OUTPUTS[key]))

        self.refresh_debug()

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
        # Hot-reload: push new coefficients to running clients immediately
        # so the next wind calculation uses updated values without reconnect
        app.mav_client.ws_a = coeffs["ws_a"]
        app.mav_client.ws_b = coeffs["ws_b"]
        app.sim.ws_a = coeffs["ws_a"]
        app.sim.ws_b = coeffs["ws_b"]
        # Replay client is created by another work stream — push only if present
        replay_client = getattr(app, "replay_client", None)
        if replay_client is not None:
            replay_client.ws_a = coeffs["ws_a"]
            replay_client.ws_b = coeffs["ws_b"]
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
        # Replay client is created by another work stream — push only if present
        replay_client = getattr(app, "replay_client", None)
        if replay_client is not None:
            replay_client.ws_a = DEFAULT_WIND_COEFFS["ws_a"]
            replay_client.ws_b = DEFAULT_WIND_COEFFS["ws_b"]
        fb = self.ids.get('wind_feedback')
        if fb:
            fb.text = "Reset to defaults"

    # -- Remote ID --

    def odid_id_filter(self, substring, from_undo):
        """TextInput input_filter: drop path/CSV-unsafe characters as typed."""
        return _sanitize_id(substring)

    def _push_odid(self, operator_id, drone_serial):
        """Hot-reload operator identity onto the live and replay clients."""
        app = App.get_running_app()
        for client in (app.mav_client, getattr(app, "replay_client", None)):
            if client is not None:
                client.operator_id = operator_id
                client.drone_serial = drone_serial

    def apply_odid(self):
        app = App.get_running_app()
        odid = {}
        for key, widget_id in self._ODID_FIELDS:
            inp = self.ids.get(widget_id)
            odid[key] = _sanitize_id(inp.text.strip()) if inp else ODID_DEFAULTS[key]
        app.settings_data["odid"] = odid
        _save_settings(app.settings_data)
        self._push_odid(odid["operator_id"], odid["drone_serial"])
        fb = self.ids.get('odid_feedback')
        if fb:
            fb.text = "Remote ID saved"

    def reset_odid_defaults(self):
        app = App.get_running_app()
        app.settings_data["odid"] = dict(ODID_DEFAULTS)
        _save_settings(app.settings_data)
        for key, widget_id in self._ODID_FIELDS:
            inp = self.ids.get(widget_id)
            if inp:
                inp.text = ODID_DEFAULTS[key]
        self._push_odid(ODID_DEFAULTS["operator_id"], ODID_DEFAULTS["drone_serial"])
        fb = self.ids.get('odid_feedback')
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

    # -- Replay --

    # (settings key, KV switch id) for each replay-output toggle.  Drives both
    # the on_enter sync and the generic on_active handler below.
    _REPLAY_SWITCHES = [
        ("replay_generate_debug_log", "replay_debug_log_switch"),
        ("replay_generate_raw",       "replay_raw_switch"),
        ("replay_generate_alm",       "replay_alm_switch"),
        ("replay_generate_tim",       "replay_tim_switch"),
        ("replay_generate_wmo",       "replay_wmo_switch"),
    ]

    def on_replay_output_toggle(self, key, active):
        """Persist one replay-output toggle.  Affects replay only, not live.

        Shared by all five replay switches; ``key`` is the settings key.  The
        on_enter sync sets each switch programmatically, which also fires
        on_active — the equality check skips the save/feedback when nothing
        actually changed.
        """
        active = bool(active)
        app = App.get_running_app()
        default = DEFAULT_REPLAY_OUTPUTS.get(key, False)
        if active == bool(app.settings_data.get(key, default)):
            return
        app.settings_data[key] = active
        _save_settings(app.settings_data)
        fb = self.ids.get("replay_feedback")
        if fb:
            fb.text = "Replay output settings saved"

    def update(self, state):
        pass

    def refresh_debug(self):
        from gcs.logutil import get_recent_logs
        from gcs import storage_paths
        app = App.get_running_app()

        logger = getattr(app.mav_client, "_msg_logger", None)
        log_path = logger.path if logger else None
        pl = self.ids.get("debug_path")
        if pl:
            pl.text = f"Log: {log_path}" if log_path else "Log: (not open)"

        dl = self.ids.get("debug_log")
        if dl:
            lines = get_recent_logs()[-200:]
            dl.text = storage_paths.report() + "\n\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Parameter Editor Screen
# ---------------------------------------------------------------------------

class ParamRow(BoxLayout):
    """Single row in the parameter list."""
    param_name = StringProperty('')
    param_value = StringProperty('')
    param_type_str = StringProperty('')
    is_modified = BooleanProperty(False)


class ParamsScreen(Screen):
    """ArduPilot parameter editor: read/write all drone parameters.

    Uses EventBus subscription to receive PARAM_RECEIVED events from the
    MAVLink client thread.  Parameters are loaded in bulk on refresh,
    then individual writes are verified via read-back ACK.
    """

    # MAVLink parameter type codes -> human-readable names
    _TYPE_NAMES = {
        1: "UINT8",  2: "INT8",  3: "UINT16", 4: "INT16",
        5: "UINT32", 6: "INT32", 7: "UINT64", 8: "INT64",
        9: "FLOAT", 10: "DOUBLE",
    }
    _INT_TYPES = {1, 2, 3, 4, 5, 6, 7, 8}  # types that should display as integers

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._params = {}            # {name: {value, type, index}} all received params
        self._original_values = {}   # {name: float} values at load time (for diff)
        self._modified = {}          # {name: float} user edits pending write
        self._param_count = 0        # total param count reported by vehicle
        self._loading = False        # True during bulk param download
        self._timeout_event = None   # watchdog timer for stalled downloads
        self._search_text = ""
        self._search_debounce_event = None  # debounce timer for search input
        self._subscribed = False     # EventBus subscription state
        self._page = 0               # current page in paginated list
        self._page_size = 50
        self._filtered_names = []
        self._load_retry_count = 0   # retry counter for param download
        self._MAX_RETRIES = 2        # max retries before giving up

    def on_enter(self):
        if not self._subscribed:
            app = App.get_running_app()
            app.event_bus.subscribe(EventType.PARAM_RECEIVED,
                                    self._on_param_received)
            self._subscribed = True

    def on_leave(self):
        if self._subscribed:
            app = App.get_running_app()
            app.event_bus.unsubscribe(EventType.PARAM_RECEIVED,
                                      self._on_param_received)
            self._subscribed = False

    # ── EventBus callback (runs on main thread) ──

    def _on_param_received(self, data):
        name = data["param_id"]
        value = data["param_value"]
        ptype = data["param_type"]
        index = data["param_index"]
        count = data["param_count"]

        self._param_count = count
        self._params[name] = {"value": value, "type": ptype, "index": index}

        if name not in self._original_values:
            self._original_values[name] = value

        # Update progress
        received = len(self._params)
        progress = self.ids.get("progress_bar")
        if progress and count > 0:
            progress.max = count
            progress.value = received

        count_label = self.ids.get("param_count_label")
        if count_label:
            count_label.text = f"{received} / {count}"

        # Write-ack verification: after a single param write, the vehicle
        # echoes the new value back.  We compare to confirm the write took.
        if not self._loading and name in self._modified:
            if abs(self._modified[name] - value) < 1e-6:
                del self._modified[name]
                self._original_values[name] = value
                self._update_write_button()
                self._update_row_highlight(name)
                fb = self.ids.get("params_feedback")
                if fb:
                    fb.text = f"Written: {name} = {self._format_value(value, ptype)}"
            else:
                fb = self.ids.get("params_feedback")
                if fb:
                    fb.text = f"Write FAILED for {name}: vehicle reports {value}"

        # Check bulk load completion
        if self._loading and received >= count:
            self._loading = False
            if self._timeout_event:
                self._timeout_event.cancel()
                self._timeout_event = None
            self._rebuild_param_list()
            fb = self.ids.get("params_feedback")
            if fb:
                fb.text = f"Loaded {received} parameters"
            self.ids.refresh_btn.disabled = False

        # Reset timeout on each received param during loading
        if self._loading:
            if self._timeout_event:
                self._timeout_event.cancel()
            self._timeout_event = Clock.schedule_once(
                self._on_load_timeout, 5.0)

    def _on_load_timeout(self, dt):
        self._timeout_event = None
        received = len(self._params)
        fb = self.ids.get("params_feedback")

        if self._load_retry_count < self._MAX_RETRIES:
            self._load_retry_count += 1
            if fb:
                fb.text = (f"Timeout: {received}/{self._param_count} received. "
                           f"Retrying ({self._load_retry_count}/{self._MAX_RETRIES})...")
            app = App.get_running_app()
            app.mav_client.request_all_params()
            self._timeout_event = Clock.schedule_once(
                self._on_load_timeout, 10.0)
            return

        self._loading = False
        self._rebuild_param_list()
        if fb:
            fb.text = (f"Timeout: received {received}/{self._param_count} params. "
                       f"Press Refresh to retry.")
        self.ids.refresh_btn.disabled = False

    # ── UI actions ──

    def on_refresh(self):
        app = App.get_running_app()
        if not app.mav_client.running:
            fb = self.ids.get("params_feedback")
            if fb:
                fb.text = "Not connected to vehicle"
            return

        self._params.clear()
        self._original_values.clear()
        self._modified.clear()
        self._param_count = 0
        self._loading = True
        self._load_retry_count = 0
        self._search_text = ""
        self._page = 0
        self._filtered_names = []

        search = self.ids.get("search_input")
        if search:
            search.text = ""
        param_list = self.ids.get("param_list")
        if param_list:
            param_list.clear_widgets()
        progress = self.ids.get("progress_bar")
        if progress:
            progress.value = 0
            progress.max = 100
        self._update_write_button()

        fb = self.ids.get("params_feedback")
        if fb:
            fb.text = "Loading parameters..."
        self.ids.refresh_btn.disabled = True

        self._timeout_event = Clock.schedule_once(
            self._on_load_timeout, 10.0)

        app.mav_client.request_all_params()

    def on_param_edited(self, name, new_text):
        if name not in self._params:
            return

        ptype = self._params[name]["type"]
        try:
            if ptype in self._INT_TYPES:
                new_value = float(int(float(new_text)))
            else:
                new_value = float(new_text)
        except ValueError:
            fb = self.ids.get("params_feedback")
            if fb:
                fb.text = f"Invalid value for {name}: '{new_text}'"
            return

        original = self._original_values.get(name, self._params[name]["value"])

        if abs(new_value - original) > 1e-7:
            self._modified[name] = new_value
        elif name in self._modified:
            del self._modified[name]

        self._update_write_button()
        self._update_row_highlight(name)

        fb = self.ids.get("params_feedback")
        if fb:
            mod_count = len(self._modified)
            if mod_count > 0:
                fb.text = f"{mod_count} parameter(s) modified"
            else:
                fb.text = "No changes"

    def on_get_log(self):
        """Fetch the drone's most recent LOG.BIN (SoW 205195 #12).

        The download runs on the client's IO thread; both callbacks
        arrive there and are rescheduled onto the Kivy main thread.
        """
        app = App.get_running_app()
        fb = self.ids.get("params_feedback")
        btn = self.ids.get("get_log_btn")
        if not app.mav_client.running:
            if fb:
                fb.text = "Not connected to vehicle"
            return

        def _ui(text, enable_btn=None):
            def _apply(_dt):
                if fb:
                    fb.text = text
                if btn is not None and enable_btn is not None:
                    btn.disabled = not enable_btn
            Clock.schedule_once(_apply, 0)

        def _on_progress(pct, total):
            _ui(f"Downloading log: {pct:.0f}% of {total // 1024} KB")

        def _on_done(success, msg):
            if success:
                _ui(f"Log saved: {msg}", enable_btn=True)
            else:
                _ui(f"Get Log failed: {msg}", enable_btn=True)

        if btn:
            btn.disabled = True
        if fb:
            fb.text = "Requesting log list…"
        app.mav_client.fetch_log(on_progress=_on_progress,
                                 on_done=_on_done)

    def on_write_params(self):
        if not self._modified:
            return
        app = App.get_running_app()
        if not app.mav_client.running:
            fb = self.ids.get("params_feedback")
            if fb:
                fb.text = "Not connected to vehicle"
            return
        self._confirm_write(app)

    def _confirm_write(self, app):
        from kivy.uix.popup import Popup
        from kivy.uix.label import Label
        from kivy.uix.button import Button
        from kivy.uix.scrollview import ScrollView

        count = len(self._modified)
        lines = []
        for name, val in sorted(self._modified.items()):
            ptype = self._params[name]["type"]
            original = self._original_values.get(name, self._params[name]["value"])
            lines.append(f"{name}: {self._format_value(original, ptype)} -> "
                         f"{self._format_value(val, ptype)}")
        summary = "\n".join(lines[:20])
        if count > 20:
            summary += f"\n... and {count - 20} more"

        content = BoxLayout(orientation='vertical', padding=10, spacing=10)

        scroll = ScrollView(size_hint_y=0.7, do_scroll_x=False)
        lbl = Label(
            text=summary, font_size='11sp',
            color=list(get_color("text_primary")),
            halign='left', valign='top',
            size_hint_y=None)
        lbl.bind(texture_size=lambda inst, sz: setattr(inst, 'height', sz[1]))
        lbl.bind(size=lambda inst, val: setattr(inst, 'text_size', (inst.width, None)))
        scroll.add_widget(lbl)
        content.add_widget(scroll)

        btn_row = BoxLayout(size_hint_y=None, height=44, spacing=10)
        popup = Popup(
            title=f'Write {count} Parameter(s)?',
            content=content,
            size_hint=(0.7, 0.6),
            auto_dismiss=False)

        yes_btn = Button(text='Write', background_color=list(get_color("btn_apply")))
        no_btn = Button(text='Cancel', background_color=list(get_color("btn_clear")))

        yes_btn.bind(on_release=lambda *_: (popup.dismiss(), self._do_write(app)))
        no_btn.bind(on_release=lambda *_: popup.dismiss())

        btn_row.add_widget(yes_btn)
        btn_row.add_widget(no_btn)
        content.add_widget(btn_row)
        popup.open()

    def _do_write(self, app):
        fb = self.ids.get("params_feedback")
        count = len(self._modified)
        for name, value in list(self._modified.items()):
            ptype = self._params[name]["type"]
            app.mav_client.set_param(name, value, param_type=ptype)
        if fb:
            fb.text = f"Writing {count} parameter(s)... waiting for ACK"

    def on_search_changed(self, text):
        # Debounce: rebuild the widget list 300ms after the last keystroke
        # instead of on every character, preventing UI freezes with 800+ params.
        if self._search_debounce_event:
            self._search_debounce_event.cancel()
        self._search_text = text.strip().upper()
        self._page = 0
        self._search_debounce_event = Clock.schedule_once(
            lambda dt: self._rebuild_param_list(), 0.3)

    # ── Internal helpers ──

    def _format_value(self, value, ptype):
        if ptype in self._INT_TYPES:
            return str(int(value))
        return f"{value:.6f}".rstrip("0").rstrip(".")

    def _rebuild_param_list(self):
        param_list = self.ids.get("param_list")
        if not param_list:
            return
        param_list.clear_widgets()

        # Build filtered list
        sorted_names = sorted(self._params.keys())
        if self._search_text:
            sorted_names = [n for n in sorted_names
                            if self._search_text in n.upper()]
        self._filtered_names = sorted_names

        # Pagination
        total = len(sorted_names)
        total_pages = max(1, (total + self._page_size - 1) // self._page_size)
        if self._page >= total_pages:
            self._page = total_pages - 1
        if self._page < 0:
            self._page = 0

        start = self._page * self._page_size
        end = min(start + self._page_size, total)
        page_names = sorted_names[start:end]

        for name in page_names:
            info = self._params[name]
            ptype = info["type"]
            value = self._modified.get(name, info["value"])
            type_name = self._TYPE_NAMES.get(ptype, f"T{ptype}")

            row = ParamRow()
            row.param_name = name
            row.param_value = self._format_value(value, ptype)
            row.param_type_str = type_name
            row.is_modified = name in self._modified

            def _bind(dt, r=row, n=name):
                inp = r.ids.get('value_input')
                if inp:
                    inp.bind(on_text_validate=lambda inst, pn=n: self.on_param_edited(pn, inst.text))

            Clock.schedule_once(_bind, 0)
            param_list.add_widget(row)

        # Update pagination controls
        self._update_pagination(total, total_pages)

    def _update_pagination(self, total, total_pages):
        page_label = self.ids.get("page_label")
        if page_label:
            page_label.text = f"Page {self._page + 1} / {total_pages}  ({total} params)"

        prev_btn = self.ids.get("prev_btn")
        if prev_btn:
            prev_btn.disabled = self._page <= 0

        next_btn = self.ids.get("next_btn")
        if next_btn:
            next_btn.disabled = self._page >= total_pages - 1

    def on_prev_page(self):
        if self._page > 0:
            self._page -= 1
            self._rebuild_param_list()

    def on_next_page(self):
        total = len(self._filtered_names)
        total_pages = max(1, (total + self._page_size - 1) // self._page_size)
        if self._page < total_pages - 1:
            self._page += 1
            self._rebuild_param_list()

    def _update_write_button(self):
        btn = self.ids.get("write_btn")
        if btn:
            btn.disabled = len(self._modified) == 0

    def _update_row_highlight(self, name):
        param_list = self.ids.get("param_list")
        if not param_list:
            return
        for child in param_list.children:
            if hasattr(child, 'param_name') and child.param_name == name:
                child.is_modified = name in self._modified
                break

    def update(self, state):
        pass


# ---------------------------------------------------------------------------
# Load KV layout file — MUST happen after all Screen/Widget class
# definitions above so the KV parser can resolve class references.
# The KV file defines the visual layout and binds to theme_* properties.
# ---------------------------------------------------------------------------
Builder.load_file(_KV_PATH)


# ═══════════════════════════════════════════════════════════════════════════
# App
# ═══════════════════════════════════════════════════════════════════════════

class CopterSondeGCSApp(App):
    title = "CopterSonde GCS"

    # ── Theme property system ─────────────────────────────────────────
    # Each ListProperty below is bound to color attributes in the KV
    # file via `app.theme_*`.  When apply_theme() updates these
    # properties, Kivy's property binding system automatically redraws
    # every widget that references them — no manual invalidation needed.
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
    theme_btn_toggle_on = ListProperty([0.15, 0.5, 0.2, 1])
    theme_btn_toggle_off = ListProperty([0.6, 0.18, 0.18, 1])
    theme_btn_nav_active = ListProperty([0.2, 0.4, 0.7, 1])
    theme_tile_default = ListProperty([0.18, 0.18, 0.22, 1])
    theme_tile_border = ListProperty([0.3, 0.3, 0.35, 1])

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
        self.theme_btn_toggle_on = list(get_color("btn_toggle_on"))
        self.theme_btn_toggle_off = list(get_color("btn_toggle_off"))
        self.theme_btn_nav_active = list(get_color("btn_nav_active"))
        self.theme_tile_default = list(get_color("tile_default"))
        self.theme_tile_border = list(get_color("tile_border"))
        # Re-highlight the active nav button after theme change
        self._update_nav_buttons()

    def set_app_theme(self, name):
        """Switch theme, persist choice, and refresh UI."""
        set_theme(name)
        self.settings_data["theme"] = name
        _save_settings(self.settings_data)
        self.apply_theme()

    def build(self):
        # Load persisted settings (connection, thresholds, theme, etc.)
        self.settings_data = _load_settings()

        # Migrate the old single replay-log toggle to its renamed key.  The old
        # "replay_generates_logs" only ever controlled the Debug MAVLink dump,
        # which is now "replay_generate_debug_log"; carry a saved value across
        # so an existing preference is preserved.
        if ("replay_generates_logs" in self.settings_data
                and "replay_generate_debug_log" not in self.settings_data):
            self.settings_data["replay_generate_debug_log"] = (
                self.settings_data.pop("replay_generates_logs"))

        # Apply persisted theme before any widget is created
        theme_name = self.settings_data.get("theme", "dark")
        set_theme(theme_name)
        self.apply_theme()

        # Shared state and event bus — these are the central data conduits.
        # VehicleState holds all telemetry; EventBus dispatches typed events
        # (e.g. PARAM_RECEIVED) from worker threads to the main thread.
        self.event_bus = EventBus()
        self.vehicle_state = VehicleState()

        # MAVLink client — runs on a background thread
        self.mav_client = MAVLinkClient(
            port=DEFAULT_PORT,
            state=self.vehicle_state,
            event_bus=self.event_bus,
        )

        # Simulated telemetry for demo mode (no vehicle required)
        self.sim = SimTelemetry(
            state=self.vehicle_state,
            event_bus=self.event_bus,
        )

        # Telemetry-log replay client (interface contract item 4) —
        # shares the same state/event bus as the live client; the
        # Connection screen keeps the three sources mutually exclusive.
        self.replay_client = TlogReplayClient(
            state=self.vehicle_state,
            event_bus=self.event_bus,
        )

        # Device GPS -> Remote ID operator location (SoW 205195 #37).
        # Started on Android once location permission is granted; other
        # platforms have no location source and broadcast "unknown".
        self.device_location = DeviceLocation(on_fix=self._on_device_fix)

        # Clock event handle for the periodic UI refresh loop
        self.update_event = None

        # Restore persisted wind coefficients for all telemetry sources
        wind = self.settings_data.get("wind_coeffs", {})
        self.mav_client.ws_a = wind.get("ws_a", DEFAULT_WIND_COEFFS["ws_a"])
        self.mav_client.ws_b = wind.get("ws_b", DEFAULT_WIND_COEFFS["ws_b"])
        self.sim.ws_a = wind.get("ws_a", DEFAULT_WIND_COEFFS["ws_a"])
        self.sim.ws_b = wind.get("ws_b", DEFAULT_WIND_COEFFS["ws_b"])
        self.replay_client.ws_a = wind.get("ws_a", DEFAULT_WIND_COEFFS["ws_a"])
        self.replay_client.ws_b = wind.get("ws_b", DEFAULT_WIND_COEFFS["ws_b"])

        # Restore persisted operator identity for the live and replay clients
        # (the sim produces no operator-identity output, so it is skipped).
        odid = self.settings_data.get("odid", {})
        op_id = _sanitize_id(odid.get("operator_id", ODID_DEFAULTS["operator_id"]))
        serial = _sanitize_id(odid.get("drone_serial", ODID_DEFAULTS["drone_serial"]))
        for client in (self.mav_client, self.replay_client):
            client.operator_id = op_id
            client.drone_serial = serial

        # Restore persisted MAVLink stream request rate
        self.mav_client.stream_rate_hz = self.settings_data.get(
            "stream_rate_hz", DEFAULT_STREAM_RATE_HZ)

        root = GCSRoot()
        return root

    # ── Per-screen update rate throttling ────────────────────────────
    # High-priority screens (flight, sensor_plots, profile) are not
    # listed here and update at the full UI_UPDATE_HZ (10 Hz).
    # Lower-priority screens are throttled to reduce CPU/GPU load,
    # especially on Android where battery life matters.
    _SCREEN_INTERVALS = {
        "profile": 0.5,      # ~2 Hz — full-history iteration is expensive
        "map": 0.25,         # ~4 Hz — tile rendering is expensive
        "connection": 0.5,   # ~2 Hz — mostly static UI
        "params": 0.5,       # ~2 Hz — only changes on bulk load
        "settings": 0.5,     # ~2 Hz — user-driven changes only
    }

    def on_start(self):
        """Called after build -- the widget tree from KV is ready."""
        # Add all screens to the ScreenManager (order = swipe order)
        sm = self.root.ids.sm
        sm.transition = SlideTransition(duration=0.2)
        sm.add_widget(ConnectionScreen(name="connection"))
        sm.add_widget(FlightScreen(name="flight"))
        sm.add_widget(SensorPlotScreen(name="sensor_plots"))
        sm.add_widget(ProfileScreen(name="profile"))
        sm.add_widget(MapScreen(name="map"))
        sm.add_widget(ParamsScreen(name="params"))
        sm.add_widget(SettingsScreen(name="settings"))
        self.sm = sm
        # Tracks last update time per screen for rate throttling
        self._screen_last_update = {}

        # Highlight the initial nav button (connection is the first screen)
        self._update_nav_buttons()

        # ── Android storage permission flow ──────────────────────────
        # Deferred by one frame so the UI is fully rendered before the
        # system permission dialog appears.
        if ON_ANDROID:
            Clock.schedule_once(self._request_android_permissions, 0)

    def _on_device_fix(self, lat, lon):
        """Device GPS fix (platform thread) — atomic assignment only."""
        self.mav_client.operator_location = (lat, lon)

    def _request_android_permissions(self, dt):
        """Request runtime permissions on Android 6+ (storage, location).

        We check first (in case already granted from a previous run) and
        request only what is missing, in a single dialog flow.  Each
        capability is enabled independently from its own grant: a denied
        location must not block the storage tree, or vice versa.
        """
        try:
            from android.permissions import (  # type: ignore
                request_permissions, check_permission, Permission,
            )
            needed = []
            if check_permission(Permission.WRITE_EXTERNAL_STORAGE):
                log.info("Storage permission already granted")
            else:
                needed += [Permission.WRITE_EXTERNAL_STORAGE,
                           Permission.READ_EXTERNAL_STORAGE]
            # COARSE is requested alongside FINE: Android 12+ auto-denies
            # a FINE-only request, and a coarse fix is still a usable
            # operator location.
            if (check_permission(Permission.ACCESS_FINE_LOCATION)
                    or check_permission(Permission.ACCESS_COARSE_LOCATION)):
                log.info("Location permission already granted")
                self.device_location.start()
            else:
                needed += [Permission.ACCESS_FINE_LOCATION,
                           Permission.ACCESS_COARSE_LOCATION]
            if needed:
                log.info("Requesting permissions: %s", needed)
                request_permissions(needed,
                                    callback=self._permission_callback)
        except Exception:
            log.exception("Failed to request Android permissions")

    def _permission_callback(self, permissions, grant_results):
        """Called asynchronously after the user responds to the permission dialog.

        Must schedule back to main thread since Android callbacks run on
        a different thread.
        """
        from android.permissions import Permission  # type: ignore
        results = dict(zip(permissions, grant_results))
        storage = results.get(Permission.WRITE_EXTERNAL_STORAGE)
        if storage is True:
            log.info("Storage permissions granted")
        elif storage is False:
            log.warning("Storage permissions denied — using app-private storage")
        fine = results.get(Permission.ACCESS_FINE_LOCATION)
        coarse = results.get(Permission.ACCESS_COARSE_LOCATION)
        if fine is True or coarse is True:
            log.info("Location permission granted (fine=%s coarse=%s)",
                     fine, coarse)
            Clock.schedule_once(lambda dt: self.device_location.start(), 0)
        elif fine is False or coarse is False:
            log.warning("Location permission denied — Remote ID operator "
                        "location will be 'unknown'")

    def switch_screen(self, name):
        self.root.ids.sm.current = name
        self._update_nav_buttons()

    def _update_nav_buttons(self):
        """Highlight the active nav button blue, reset others."""
        if not self.root:
            return
        navbar = self.root.ids.get('navbar')
        if not navbar:
            return
        current = self.root.ids.sm.current
        for btn in navbar.children:
            if hasattr(btn, 'screen_name'):
                if btn.screen_name == current:
                    btn.background_color = self.theme_btn_nav_active
                else:
                    btn.background_color = self.theme_bg_input

    def update_ui(self, _dt):
        """Periodic UI refresh -- delegates to the current screen.

        High-priority screens (flight, sensor_plots, profile) update every
        tick (10 Hz).  Lower-priority screens are throttled per
        _SCREEN_INTERVALS to reduce CPU load on constrained hardware.
        Only the currently visible screen is updated to save resources.
        """
        screen = self.sm.current_screen
        if not hasattr(screen, "update"):
            return
        # Apply per-screen throttling if configured
        interval = self._SCREEN_INTERVALS.get(screen.name)
        if interval is not None:
            now = time.monotonic()
            last = self._screen_last_update.get(screen.name, 0.0)
            if now - last < interval:
                return  # too soon — skip this tick
            self._screen_last_update[screen.name] = now
        screen.update(self.vehicle_state)

    def on_pause(self):
        # Cancel UI refresh while paused to prevent stale data accumulation
        # from overwhelming the profile screen on resume.
        self._was_refreshing = self.update_event is not None
        if self.update_event:
            self.update_event.cancel()
            self.update_event = None
        return True

    def on_resume(self):
        # Only restart the UI refresh if it was running before pause.
        if getattr(self, '_was_refreshing', False) and self.update_event is None:
            self.update_event = Clock.schedule_interval(
                self.update_ui, 1.0 / UI_UPDATE_HZ
            )

    def on_stop(self):
        log.info("Application stopping – cleaning up…")
        if self.update_event:
            self.update_event.cancel()
        self.mav_client.stop()
        self.sim.stop()
        self.replay_client.stop()
        self.device_location.stop()


def main():
    CopterSondeGCSApp().run()


if __name__ == "__main__":
    main()
