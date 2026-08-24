"""
CopterSonde Ground Control Station – Kivy application entry point.

Multi-screen GCS app with bottom navigation bar.
"""

import datetime
import json
import math
import os
import struct
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
# TEMPORARY (SoW #51, remove before production): compliance GPS logger
from gcs.compliance_gps_logger import ComplianceGpsLogger  # noqa: E402
# TEMPORARILY DISABLED: battery-voltage web dashboard PoC.
# from gcs.web_dashboard import WebDashboard  # noqa: E402
from app.hud_widget import FlightHUD  # noqa: E402,F401
from app.plot_widget import TimeSeriesPlot, ProfilePlot  # noqa: E402,F401
from app.map_widget import MapWidget  # noqa: E402,F401
from app.tlog_picker import open_tlog_picker  # noqa: E402
from app.device_location import DeviceLocation  # noqa: E402
from app.theme import get_color, get_color_hex, set_theme, get_theme_name, THEME_NAMES  # noqa: E402

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
# Settings persistence: named preset files + real-time autosave
# (SoW 205195 §1.8, #23–#29)
# ---------------------------------------------------------------------------
# All user preferences (thresholds, wind coefficients, theme choice, stream
# rate, Remote ID identity, replay toggles, checklist choice) live in
# ``app.settings_data`` — a plain dict, unchanged from the previous design —
# and are persisted under [program data]/Settings (SoW #27).
#
# File layout in that folder:
#   <PresetName>.json   named preset files, written only by an explicit
#                       "Save to Preset" / "Save As New" (SoW #23)
#   _autosave.json      real-time placeholder written on *every* settings
#                       change, so nothing is lost when the Herelink is
#                       powered off without closing apps (SoW #29).  The
#                       leading underscore keeps it out of the preset list.
#   settings.json       legacy pre-preset file; read once as a migration
#                       source if no autosave exists, then ignored.
#
# Startup resolution (SoW #25/#26): _autosave.json → legacy settings.json →
# CS3.1.json preset file → in-code CS3.1 defaults.
#
# A handful of keys are *session state*, not settings-tab options (the
# Connection screen's last selection, and the preset pointer itself).  They
# ride along in the autosave but are never written into preset files.

CS31_PRESET_NAME = "CS3.1"

_SESSION_KEYS = (
    "settings_preset",    # which preset file the current settings belong to
    "last_preset",        # Connection screen: connection preset spinner
    "last_conn_type",
    "last_ip",
    "last_port",
    "gcs_tx_enabled",     # SoW #49 test gate — autosave only, never presets:
                          # a saved preset that silently disables all GCS
                          # transmissions would be a field-day landmine
)


def _settings_dir():
    return program_data_dir("Settings")


def _preset_path(name):
    return os.path.join(_settings_dir(), name + ".json")


def _autosave_path():
    return os.path.join(_settings_dir(), "_autosave.json")


def _read_json(path):
    """Return the parsed dict, or None on any failure (missing/corrupt)."""
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _atomic_write_json(path, obj):
    """Write JSON via a temp file + rename so a mid-write power loss never
    leaves a truncated file (SoW #29: don't count on clean shutdown)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def _preset_payload(data):
    """The subset of settings_data that belongs in a preset file."""
    return {k: v for k, v in data.items() if k not in _SESSION_KEYS}


def _normalized_payload(data):
    """JSON-round-tripped preset payload, for saved/unsaved comparison."""
    return json.loads(json.dumps(_preset_payload(data), sort_keys=True))


def _cs31_settings():
    """In-code values for the CS3.1 default preset (SoW #24, #26).

    The thresholds are the SoW #24 values (DEFAULT_THRESHOLDS); the
    remaining keys carry the application defaults.  Note that a valid
    CS3.1.json already on disk is never overwritten by _seed_cs31_preset(),
    so a device seeded before the #24 values landed keeps its old file
    until CS3.1.json is deleted (re-seeds on next launch) or re-saved.
    """
    from gcs import checklists
    d = {
        "thresholds": dict(DEFAULT_THRESHOLDS),
        "wind_coeffs": dict(DEFAULT_WIND_COEFFS),
        "odid": dict(ODID_DEFAULTS),
        "theme": "dark",
        "stream_rate_hz": DEFAULT_STREAM_RATE_HZ,
        "checklist_file": checklists.DEFAULT_FILENAME,
    }
    d.update(DEFAULT_REPLAY_OUTPUTS)
    return d


def _seed_cs31_preset():
    """Create CS3.1.json from the in-code defaults if it is missing or
    unreadable.  A valid file — including one the user has re-saved with
    their own values — is never touched."""
    p = _preset_path(CS31_PRESET_NAME)
    if _read_json(p) is None:
        try:
            _atomic_write_json(p, _cs31_settings())
        except OSError as e:
            log.error("Failed to seed %s preset at %s: %s",
                      CS31_PRESET_NAME, p, e)


def _list_presets():
    """Names (without .json) of the preset files on disk, sorted."""
    try:
        names = []
        for fn in os.listdir(_settings_dir()):
            if not fn.endswith(".json"):
                continue
            stem = fn[:-len(".json")]
            # skip the autosave/placeholder files and the legacy file
            if stem.startswith("_") or fn == "settings.json":
                continue
            names.append(stem)
        return sorted(names, key=str.lower)
    except OSError:
        return []


def _load_preset_snapshot(name):
    """Normalized payload of a preset file, or None if unreadable."""
    d = _read_json(_preset_path(name))
    if d is None:
        return None
    return json.loads(json.dumps(
        {k: v for k, v in d.items() if k not in _SESSION_KEYS},
        sort_keys=True))


def _load_settings():
    """Resolve the working settings at startup (SoW #25/#26).

    Order: autosave (last state, including any unsaved edits) → legacy
    single-file settings.json (one-time migration source) → the CS3.1
    preset file → in-code CS3.1 defaults.
    """
    data = _read_json(_autosave_path())
    if data is None:
        data = _read_json(os.path.join(_settings_dir(), "settings.json"))
    if data is None:
        data = _read_json(_preset_path(CS31_PRESET_NAME))
        if data is not None:
            data["settings_preset"] = CS31_PRESET_NAME
    if data is None:
        data = _cs31_settings()
        data["settings_preset"] = CS31_PRESET_NAME
    data.setdefault("settings_preset", CS31_PRESET_NAME)
    return data


def _save_settings(data):
    """Persist the working settings in real time (SoW #29).

    Writes the autosave placeholder only — named preset files are written
    solely by the explicit save actions on the Settings screen.  Same
    signature as before, so all existing call sites are unchanged.
    """
    try:
        _atomic_write_json(_autosave_path(), data)
    except Exception as e:
        log.error("Failed to save settings to %s: %s", _autosave_path(), e)


def _apply_settings_dict(app, new_data, preset_name):
    """Replace the settings-tab options with ``new_data`` (a loaded preset),
    preserving session keys, and hot-reload dependent runtime state exactly
    as App.build() does on startup."""
    data = app.settings_data
    preserved = {k: data[k] for k in _SESSION_KEYS if k in data}
    data.clear()
    data.update(new_data)
    data.update(preserved)
    data["settings_preset"] = preset_name
    _save_settings(data)

    # Wind coefficients -> all telemetry sources
    wind = data.get("wind_coeffs", {})
    ws_a = wind.get("ws_a", DEFAULT_WIND_COEFFS["ws_a"])
    ws_b = wind.get("ws_b", DEFAULT_WIND_COEFFS["ws_b"])
    for client in (app.mav_client, app.sim, getattr(app, "replay_client", None)):
        if client is not None:
            client.ws_a = ws_a
            client.ws_b = ws_b

    # Remote ID operator identity -> live and replay clients
    odid = data.get("odid", {})
    op_id = _sanitize_id(odid.get("operator_id", ""))
    serial = _sanitize_id(odid.get("drone_serial", ""))
    for client in (app.mav_client, getattr(app, "replay_client", None)):
        if client is not None:
            client.operator_id = op_id
            client.drone_serial = serial

    # Stream rate (takes effect on next connection, as elsewhere)
    app.mav_client.stream_rate_hz = data.get(
        "stream_rate_hz", DEFAULT_STREAM_RATE_HZ)

    # Theme (set_app_theme persists + repaints; no-op safe if unchanged)
    app.set_app_theme(data.get("theme", "dark"))


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
            color=get_color("text_popup")))

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
    # Default evaluated at import time (theme.py's "dark"); overridden
    # from the active theme on every telemetry update.
    tile_color = ListProperty(list(get_color("tile_default")))


# ═══════════════════════════════════════════════════════════════════════════
# Pre-flight checklist items
# ═══════════════════════════════════════════════════════════════════════════
# All items must be checked before the ARM button is enabled.
# This forces the operator to manually verify each safety condition.

# Checklist items moved to gcs/checklists.py (SoW 205195 #15): loaded from
# [program data]/Checklists/*.checklist.json, selected in Settings (#16).


# ═══════════════════════════════════════════════════════════════════════════
# Unified Flight Screen (telemetry + HUD + commands)
# ═══════════════════════════════════════════════════════════════════════════

class FlightScreen(Screen):
    """Unified flight screen: telemetry table (left half), HUD (top-right),
    commands with pre-flight checklist (bottom-right)."""

    # Remote ID readiness indicator (SoW 205195 #38), bound from KV.
    rid_text = StringProperty("Remote ID")
    # "Unknown" until the first _update_rid_indicator() on screen entry.
    rid_color = ListProperty(list(get_color("tile_unknown")))

    # "Declare Emergency" SELF_ID toggle (SoW 205195 #41/#42), bound from KV.
    emergency_text = StringProperty("DECLARE EMERGENCY")
    emergency_color = ListProperty(list(get_color("btn_danger")))

    # MAVLink STATUSTEXT severity -> message category (SoW 205195 #22).
    # Industrial standard (SoW §1.7): red errors, orange warnings, white
    # notifications.  Colors are resolved through the theme at render
    # time (SoW #20/#21) so they stay contrast-correct per theme.
    _SEV_CATEGORY = {
        0: "status_msg_error",         # EMERGENCY
        1: "status_msg_error",         # ALERT
        2: "status_msg_error",         # CRITICAL
        3: "status_msg_error",         # ERROR
        4: "status_msg_warning",       # WARNING
        5: "status_msg_notification",  # NOTICE
        6: "status_msg_notification",  # INFO
        7: "status_msg_notification",  # DEBUG
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Pre-flight checklist popup state.  (The checklist no longer
        # gates an ARM button — arming moved to the Herelink hardware —
        # it remains as an operator aid.)
        self._checklist_popup = None
        self._proceed_btn = None
        self._check_states = {}
        # Flight timer: starts on armed, stops on disarmed.
        # Tracks state transitions to avoid repeated start/stop.
        self._prev_armed = None
        self._flight_timer_start = None   # monotonic() timestamp when armed
        self._flight_timer_elapsed = 0.0  # accumulated seconds (survives pause)
        # Status message caching — only rebuild the markup string when
        # new messages arrive or the theme changes, not every UI tick.
        # Keyed on the monotonic total-received counter, not the list
        # length: the list is capped at 200 entries, so its length stops
        # changing (and would stop invalidating the cache) once full.
        self._cached_status_total = 0
        self._cached_status_theme = None
        self._cached_status_text = "No messages"

    # ── Remote ID indicator (SoW 205195 #38) ────────────────────

    def on_enter(self):
        # Refresh once on entry so the indicators are correct even before a
        # connection starts the periodic update loop.
        self._update_rid_indicator()
        self._update_emergency_button()

    def _update_rid_indicator(self):
        """Green: identity set and device GPS fix.  Red: fixable problem
        (identity missing, or no fix yet on a GPS-capable device).
        Yellow: this platform has no GPS, so green is unreachable — a
        deliberate third state beyond SoW #38 for desktop test runs.

        The red no-fix case is split three ways so the operator can act
        on it in the field: LOCATION OFF (Android Location disabled or
        in battery-saving mode — enable it in Android Settings),
        NO GPS DEVICE (the OS offers no GNSS provider), and NO GPS FIX
        (provider enabled, still waiting for satellites)."""
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
            gps_state = app.device_location.gps_provider_state()
            if gps_state == "disabled":
                text, color = "Remote ID: LOCATION OFF", "tile_red"
            elif gps_state == "missing":
                text, color = "Remote ID: NO GPS DEVICE", "tile_red"
            else:
                text, color = "Remote ID: NO GPS FIX", "tile_red"
        self.rid_text = text
        self.rid_color = list(_tile_color(color))

    # ── Declare Emergency toggle (SoW 205195 #41/#42) ────────────────

    def on_declare_emergency(self):
        """Toggle manual transmission of the Remote ID SELF_ID message.

        While the automatic health assertion (#42) is active, the toggle
        is forced ON and the press is refused, per the SoW.
        """
        if self._in_replay():
            return  # SoW #35: replay datastream is read-only
        app = App.get_running_app()
        client = app.mav_client
        if client.selfid_auto_active:
            self.ids.cmd_feedback.text = (
                "Emergency broadcast forced ON by unhealthy sensor "
                "\u2014 cannot be turned off")
            return
        if not client.running:
            self.ids.cmd_feedback.text = (
                "Not connected \u2014 emergency broadcast requires a drone link")
            return
        client.emergency_declared = not client.emergency_declared
        self.ids.cmd_feedback.text = (
            "EMERGENCY DECLARED \u2014 broadcasting Remote ID SELF_ID"
            if client.emergency_declared
            else "Emergency broadcast stopped")
        self._update_emergency_button()

    def _update_emergency_button(self):
        """Drive the toggle's text/color from the client's SELF_ID state.

        While transmitting, the button flashes red (the SoW's "obvious
        visual indicator"); the auto-asserted case (#42) is labeled AUTO
        so the operator knows the press-to-stop path is unavailable.
        """
        client = App.get_running_app().mav_client
        if client.selfid_active:
            # ~1 Hz flash, derived from the 10 Hz update tick
            flash = int(time.monotonic() * 2) % 2 == 0
            self.emergency_color = list(get_color(
                "status_msg_error" if flash else "tile_red"))
            self.emergency_text = (
                "EMERGENCY ACTIVE (AUTO)" if client.selfid_auto_active
                else "EMERGENCY ACTIVE \u2014 PRESS TO STOP")
        else:
            self.emergency_color = list(get_color("btn_danger"))
            self.emergency_text = "DECLARE EMERGENCY"

    # ── Telemetry update ──────────────────────────────────────────────

    def _update_telemetry(self, state):
        if not state.is_healthy():
            return

        # Set non-threshold tiles to theme default so they update on
        # theme change (e.g. high-contrast needs a light background).
        default = _tile_color("tile_default")
        for tid in ("tile_mode", "tile_time", "tile_voltage", "tile_current",
                     "tile_alt_rel", "tile_alt_amsl", "tile_heading",
                     "tile_gndspd", "tile_vertspd", "tile_throttle",
                     "tile_home_lat", "tile_home_lon", "tile_home_alt"):
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
        # state.vz is NED down-positive (cm/s); display keeps that sign
        # (positive = descending), matching the HUD's VS readout
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

        # Controller (home) position, SoW #46: read from the same device
        # location source that feeds Remote ID (visible in Settings >
        # Debug).  '---' until the device has produced a fix; altitude is
        # shown only when the platform supplied one.  last_fix is written
        # atomically as a tuple from a platform thread, so read it once.
        fix = App.get_running_app().device_location.last_fix
        if fix is not None:
            self.ids.tile_home_lat.value_text = f"{fix[0]:.5f}"
            self.ids.tile_home_lon.value_text = f"{fix[1]:.5f}"
        else:
            self.ids.tile_home_lat.value_text = "---"
            self.ids.tile_home_lon.value_text = "---"
        home_alt = App.get_running_app().device_location.last_alt
        self.ids.tile_home_alt.value_text = (
            f"{home_alt:.1f} m" if home_alt is not None else "---")

        # Radio (RSSI) removed per SoW #47: the Herelink SBUS setup never
        # supplies an RSSI source over MAVLink, so the tile could not show
        # real data on this hardware and was deleted rather than fixed.

        # Throttle
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

    # ── Command: arm & takeoff ────────────────────────────────────────
    # Removed per InterMet direction (with the SoW ch.2 Flight-screen
    # rework): arming is performed from the Herelink hardware controls,
    # not the GCS.  The pre-flight checklist remains as an operator aid.

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
        from gcs import checklists

        # SoW #15/#16: load the checklist selected in Settings, fresh on
        # every open so file edits show up without a restart.  A missing,
        # malformed, or empty checklist yields a blank window (per the
        # SoW) that can still be proceeded through.
        app = App.get_running_app()
        selected = app.settings_data.get(
            "checklist_file", checklists.DEFAULT_FILENAME)
        loaded = checklists.load_checklist(selected)
        title, items = loaded if loaded else ("Checklist", [])

        content = BoxLayout(orientation='vertical', padding=10, spacing=8)

        content.add_widget(Label(
            text='Complete all items before flight',
            font_size='14sp', size_hint_y=None, height=30,
            color=get_color("text_popup")))

        scroll = ScrollView(do_scroll_y=True, do_scroll_x=False)
        checklist_box = BoxLayout(
            orientation='vertical', size_hint_y=None, spacing=6,
            padding=[0, 4, 0, 4])
        checklist_box.bind(minimum_height=checklist_box.setter('height'))

        self._check_states = {}
        for i, item_text in enumerate(items):
            row = BoxLayout(size_hint_y=None, height=36, spacing=8)
            cb = CheckBox(size_hint_x=None, width=36, active=False)
            lbl = Label(
                text=item_text, font_size='12sp',
                color=get_color("text_popup"),
                halign='left', valign='middle')
            lbl.bind(size=lambda inst, val: setattr(
                inst, 'text_size', (inst.width, None)))
            self._check_states[i] = cb
            cb.bind(active=lambda inst, val: self._update_proceed_btn())
            row.add_widget(cb)
            row.add_widget(lbl)
            checklist_box.add_widget(row)
        if not items:
            checklist_box.add_widget(Label(
                text='(no checklist items)', font_size='12sp',
                size_hint_y=None, height=36,
                color=get_color("text_popup")))

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
            title=f'{title} Checklist', content=content,
            size_hint=(0.7, 0.8), auto_dismiss=False)

        proceed_btn.bind(
            on_release=lambda *_: self._on_checklist_proceed(popup))
        cancel_btn.bind(
            on_release=lambda *_: self._on_checklist_cancel(popup))

        btn_row.add_widget(proceed_btn)
        btn_row.add_widget(cancel_btn)
        content.add_widget(btn_row)

        self._checklist_popup = popup
        # Initialize Proceed for the current items — with zero items no
        # checkbox event ever fires, and an empty checklist is treated as
        # trivially complete (the blank window the SoW allows).
        self._update_proceed_btn()
        popup.open()

    def _update_proceed_btn(self):
        if self._proceed_btn:
            all_checked = all(
                cb.active for cb in self._check_states.values())
            self._proceed_btn.disabled = not all_checked

    def _on_checklist_proceed(self, popup):
        popup.dismiss()
        self._checklist_popup = None
        self._proceed_btn = None
        self.ids.cmd_feedback.text = "Checklist complete"

    def _on_checklist_cancel(self, popup):
        popup.dismiss()
        self._checklist_popup = None
        self._proceed_btn = None

    # ── Armed state transition management ────────────────────────────
    # Detects DISARMED->ARMED and ARMED->DISARMED transitions to:
    #   - Start/stop the flight timer
    #   - Enable/disable the checklist button
    #   - Auto-dismiss checklist popup if still open when armed

    def _update_armed_state(self, state):
        armed = state.armed
        if armed == self._prev_armed:
            return  # no transition — skip

        if armed:
            # DISARMED -> ARMED: start flight timer from zero
            self._flight_timer_start = time.monotonic()
            self._flight_timer_elapsed = 0.0
            # Lock out the checklist while flying
            self.ids.checklist_btn.disabled = True
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
            # Re-enable the checklist for the next flight
            self.ids.checklist_btn.disabled = False

        self._prev_armed = armed

    # ── Main update ───────────────────────────────────────────────────

    def update(self, state):
        # Remote ID readiness — independent of vehicle link health
        self._update_rid_indicator()

        # Declare Emergency toggle — reflects (and flashes with) the
        # client's SELF_ID assertion, including the automatic one (#42)
        self._update_emergency_button()

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

        # The bottom-left "Mode:" label was removed; flight mode is shown
        # in the MODE telemetry tile.

        # Status message caching: only rebuild the Kivy markup string
        # when new messages arrive or the theme changes (cheap checks vs
        # expensive string ops).
        # Compare the monotonic total-received counter, not len() of the
        # 200-capped list (whose length pins at 200 and never changes
        # again).  `!=` (not `>`) so a reset() that restarts the counter
        # at 0 still triggers a rebuild.
        msg_total = state.status_messages_total
        theme_name = get_theme_name()
        if (msg_total != self._cached_status_total
                or theme_name != self._cached_status_theme):
            self._cached_status_total = msg_total
            self._cached_status_theme = theme_name
            # Resolve the three category colors once per rebuild (#20/#21)
            sev_hex = {sev: get_color_hex(key)
                       for sev, key in self._SEV_CATEGORY.items()}
            notif_hex = get_color_hex("status_msg_notification")
            msgs = state.status_messages[-30:]  # show last 30 messages
            lines = []
            for sm in reversed(msgs):  # newest first
                ts = datetime.datetime.fromtimestamp(sm.timestamp).strftime(
                    "%H:%M:%S")
                # Unknown severities render as notifications
                hex_col = sev_hex.get(sm.severity, notif_hex)
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
    """ALM air temp and RH vs time since ascent start (SoW 205195 #19).

    One point per completed 5 m altitude bin; the buffers reset when a
    new ascent begins, so a finished ascent stays on screen until the
    next one starts.  x_window is 0 in the KV: the whole ascent fits.
    """

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
        # Snapshot the buffers: the IO/sim thread appends concurrently, and
        # clear_alm() swaps in new lists, so list() gives a coherent copy.
        tss = list(state.alm_tss)
        temps = list(state.alm_temp)
        rhs = list(state.alm_rh)
        n = min(len(tss), len(temps), len(rhs))

        temp_pts, rh_pts = [], []
        for i in range(n):
            if math.isfinite(temps[i]):
                temp_pts.append((tss[i], temps[i]))
            if math.isfinite(rhs[i]):
                rh_pts.append((tss[i], rhs[i]))

        temp_plot = self.ids.get('temp_plot')
        rh_plot = self.ids.get('rh_plot')
        # Series colors resolved per update tick so a theme switch
        # propagates without extra invalidation.
        if temp_plot:
            temp_plot.set_data(
                {'Temp': (get_color("plot_series_temp"), temp_pts)})
        if rh_plot:
            rh_plot.set_data(
                {'RH': (get_color("plot_series_rh"), rh_pts)})


class ProfileScreen(Screen):
    """ALM temperature, dew point, and wind profiles vs altitude ASL.

    One point per completed 5 m bin (SoW 205195 #19); buffers reset
    when a new ascent begins.  Dew point is derived per-bin from the
    ALM temp/RH since it is not itself an ALM column.
    """

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
        # Snapshot the ALM buffers (IO/sim thread appends concurrently;
        # clear_alm() swaps lists, so list() is a coherent copy).
        alts = list(state.alm_alt)
        temps = list(state.alm_temp)
        dews = list(state.alm_dew)
        wspds = list(state.alm_wspd)
        n = min(len(alts), len(temps), len(dews), len(wspds))

        _isfinite = math.isfinite

        # Temperature & Dew Point vs Altitude (ASL, from the ALM bins)
        temp_pts, dew_pts = [], []
        for i in range(n):
            if not _isfinite(alts[i]):
                continue
            if _isfinite(temps[i]):
                temp_pts.append((temps[i], alts[i]))
            if _isfinite(dews[i]):
                dew_pts.append((dews[i], alts[i]))

        temp_profile = self.ids.get('temp_profile')
        if temp_profile:
            temp_profile.set_data({
                'Temp': (get_color("plot_series_temp"),
                         self._downsample(temp_pts)),
                'Dew':  (get_color("plot_series_dew"),
                         self._downsample(dew_pts)),
            })

        # Wind Speed vs Altitude
        wspd_pts = []
        for i in range(n):
            if _isfinite(alts[i]) and _isfinite(wspds[i]):
                wspd_pts.append((wspds[i], alts[i]))

        wind_profile = self.ids.get('wind_profile')
        if wind_profile:
            wind_profile.set_data({
                'Wind Spd': (get_color("plot_series_wind"),
                             self._downsample(wspd_pts)),
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


# Alert thresholds — used to color-code telemetry tiles (green/yellow/red).
# These are the CS3.1 default-preset values specified by SoW 205195 #24.
DEFAULT_THRESHOLDS = {
    "battery_pct_warn": 50,
    "battery_pct_crit": 30,
    "voltage_min": 15.0,
    "gps_sats_min": 10,
    "hdop_max": 3.0,
    "rssi_min": 40,
    "max_wind_speed": 20.0,
    "temp_min_c": -20.0,
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
        self._sync_widgets(app)
        self._refresh_preset_ui(app)

    def _sync_widgets(self, app):
        """Push app.settings_data into every widget on the tabs.  Split out
        of on_enter so loading a preset can reuse it (SoW #23)."""
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
        # Checklist selector (SoW #16) — rescan the folder on every
        # entry so newly dropped files appear without a restart.
        self._refresh_checklist_spinner(app)
        # Replay-output toggles (replay only; never affect a live connection)
        for key, widget_id in self._REPLAY_SWITCHES:
            sw = self.ids.get(widget_id)
            if sw:
                sw.active = bool(
                    app.settings_data.get(key, DEFAULT_REPLAY_OUTPUTS[key]))
        # Testing tab: master transmit gate (SoW #49)
        tx_sw = self.ids.get("gcs_tx_switch")
        if tx_sw:
            tx_sw.active = bool(
                app.settings_data.get("gcs_tx_enabled", True))
        # Testing tab: compliance GPS logger (SoW #51, temporary) —
        # session state lives on the app, not in settings_data
        cl_sw = self.ids.get("compliance_log_switch")
        if cl_sw:
            cl_sw.active = app.compliance_gps_enabled

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

    # Reset Defaults removed per SoW 205195 #28 — redundant to reloading
    # the settings/preset file (see the presets section below).

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

    # Reset Defaults removed per SoW 205195 #28.

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

    # Reset Defaults removed per SoW 205195 #28.

    # -- Settings presets (SoW 205195 §1.8, #23–#29) --

    # Guard: _refresh_preset_ui sets the spinner text programmatically,
    # which fires on_text; the guard stops that from re-loading the file.
    _preset_guard = False

    def preset_name_filter(self, substring, from_undo):
        """TextInput input_filter for the new-preset-name field: names
        become filenames, so reuse the filename-safe character rule."""
        return _sanitize_id(substring)

    def _refresh_preset_ui(self, app):
        spinner = self.ids.get("preset_file_spinner")
        if not spinner:
            return
        current = app.settings_data.get("settings_preset", CS31_PRESET_NAME)
        self._preset_guard = True
        try:
            spinner.values = _list_presets() or [current]
            spinner.text = current
        finally:
            self._preset_guard = False
        self._update_preset_status(app)

    def _update_preset_status(self, app):
        """SoW #29: indicate whether the live settings match the selected
        preset file, or are auto-recovered/unsaved edits."""
        lbl = self.ids.get("preset_status")
        if not lbl:
            return
        name = app.settings_data.get("settings_preset", CS31_PRESET_NAME)
        snapshot = getattr(app, "preset_snapshot", None)
        if snapshot is not None and _normalized_payload(
                app.settings_data) == snapshot:
            lbl.text = f"Settings saved to preset '{name}'"
        else:
            lbl.text = (f"Auto-recovered \u2014 changes not saved to "
                        f"'{name}'. Use 'Save to Preset' to keep them.")

    def on_preset_selected(self, name):
        """Spinner selection: load that preset file (SoW #23).

        Note Kivy spinners don't fire on_text when the same entry is
        re-picked; the Reload button next to the spinner covers the
        'select the currently chosen file to discard temporary edits'
        case from #23.
        """
        if self._preset_guard or not name:
            return
        app = App.get_running_app()
        if name == app.settings_data.get("settings_preset"):
            return
        self._load_preset_into_app(name)

    def reload_selected_preset(self):
        """Re-load the selected preset file, discarding unsaved edits."""
        spinner = self.ids.get("preset_file_spinner")
        if spinner and spinner.text:
            self._load_preset_into_app(spinner.text)

    def _load_preset_into_app(self, name):
        app = App.get_running_app()
        fb = self.ids.get("preset_feedback")
        data = _read_json(_preset_path(name))
        if data is None:
            # Unreadable/missing preset: keep current settings untouched.
            if fb:
                fb.text = f"Could not load preset '{name}'"
            self._refresh_preset_ui(app)
            return
        _apply_settings_dict(app, data, name)
        app.preset_snapshot = _load_preset_snapshot(name)
        self._sync_widgets(app)
        self._refresh_preset_ui(app)
        if fb:
            fb.text = f"Loaded preset '{name}'"

    def save_current_preset(self):
        """Write the live settings to the currently selected preset file."""
        app = App.get_running_app()
        name = app.settings_data.get("settings_preset", CS31_PRESET_NAME)
        self._save_preset_named(app, name)

    def save_preset_as(self):
        """Write the live settings to a new preset file and select it."""
        app = App.get_running_app()
        inp = self.ids.get("new_preset_name")
        # lstrip protects the _autosave/_placeholder namespace and hidden
        # dotfiles; _sanitize_id already ran as the input filter.
        name = _sanitize_id(inp.text if inp else "").strip().lstrip("._")
        fb = self.ids.get("preset_feedback")
        if not name:
            if fb:
                fb.text = "Enter a preset name first"
            return
        app.settings_data["settings_preset"] = name
        self._save_preset_named(app, name)
        if inp:
            inp.text = ""
        self._refresh_preset_ui(app)

    def _save_preset_named(self, app, name):
        fb = self.ids.get("preset_feedback")
        try:
            _atomic_write_json(_preset_path(name),
                               _preset_payload(app.settings_data))
        except OSError as e:
            log.error("Failed to save preset '%s': %s", name, e)
            if fb:
                fb.text = f"Save failed: {e}"
            return
        _save_settings(app.settings_data)  # persist the preset pointer too
        app.preset_snapshot = _normalized_payload(app.settings_data)
        self._update_preset_status(app)
        if fb:
            fb.text = f"Saved preset '{name}'"

    # -- Theme --

    # -- Checklist selection (SoW 205195 #16) --

    _checklist_map = {}   # display name -> filename, rebuilt on refresh

    def _refresh_checklist_spinner(self, app):
        from gcs import checklists
        spinner = self.ids.get("checklist_spinner")
        if not spinner:
            return
        found = checklists.list_checklists()
        self._checklist_map = {name: fn for fn, name in found}
        selected = app.settings_data.get(
            "checklist_file", checklists.DEFAULT_FILENAME)
        current = next(
            (name for fn, name in found if fn == selected), None)
        if found:
            spinner.values = [name for _fn, name in found]
            spinner.text = current or "(select a checklist)"
        else:
            spinner.values = []
            spinner.text = "(none found)"

    def on_checklist_selected(self, display_name):
        # Fires for programmatic .text writes too; only persist real
        # selections from the discovered set.
        filename = self._checklist_map.get(display_name)
        if not filename:
            return
        app = App.get_running_app()
        if app.settings_data.get("checklist_file") == filename:
            return
        app.settings_data["checklist_file"] = filename
        _save_settings(app.settings_data)

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

    # -- Testing (SoW #49: temporary regulatory-testing controls) --

    def on_gcs_tx_toggle(self, active):
        """Master gate for ALL outbound GCS MAVLink (SoW #49).

        Applied to the live client immediately — including mid-connection —
        and persisted in the autosave (session key, so it never lands in a
        preset file).  The on_enter sync sets the switch programmatically,
        which also fires on_active; the equality check skips the no-change
        case, matching the replay-toggle pattern above.
        """
        active = bool(active)
        app = App.get_running_app()
        if active == bool(app.settings_data.get("gcs_tx_enabled", True)):
            return
        app.settings_data["gcs_tx_enabled"] = active
        _save_settings(app.settings_data)
        app.mav_client.tx_enabled = active
        if active:
            log.info("GCS MAVLink transmissions ENABLED (SoW #49 gate)")
        else:
            log.warning("GCS MAVLink transmissions DISABLED (SoW #49 gate) "
                        "— heartbeats, Remote ID, commands, and parameter "
                        "writes are all suppressed until re-enabled")
        fb = self.ids.get("gcs_tx_feedback")
        if fb:
            fb.text = ("Transmissions ON" if active
                       else "ALL GCS transmissions OFF")

    def on_compliance_log_toggle(self, active):
        """Compliance GPS position log (SoW #51) — TEMPORARY, remove
        before production.

        Session-only (never persisted, never in presets): every launch
        starts OFF.  Takes effect immediately — if a live connection is
        already up, logging starts now; on later connects it starts
        automatically.  The equality check skips the programmatic
        on_enter sync, matching on_gcs_tx_toggle above.
        """
        active = bool(active)
        app = App.get_running_app()
        if active == app.compliance_gps_enabled:
            return
        app.compliance_gps_enabled = active
        app.sync_compliance_logger()
        log.info("Compliance GPS logging (SoW #51) %s",
                 "ENABLED" if active else "disabled")
        fb = self.ids.get("compliance_log_feedback")
        if fb:
            if not active:
                fb.text = "Compliance GPS logging OFF"
            elif app.compliance_logger.active:
                fb.text = f"Logging to {app.compliance_logger.path}"
            else:
                fb.text = ("Compliance GPS logging armed — starts on "
                           "connect")

    def update(self, state):
        # Keep the saved/unsaved preset indicator (SoW #29) current as the
        # user edits; this screen is throttled to ~2 Hz and the comparison
        # is over a small in-memory dict, so this is cheap.
        self._update_preset_status(App.get_running_app())

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
            dl.text = ("== Remote ID / device GPS ==\n"
                       + self._gps_permission_report() + "\n"
                       + app.device_location.diagnostics() + "\n\n"
                       + storage_paths.report() + "\n\n" + "\n".join(lines))

    @staticmethod
    def _gps_permission_report():
        """One-line Android location-permission state for the Debug tab."""
        try:
            from android.permissions import check_permission, Permission  # type: ignore
        except Exception:
            return "Permission: n/a (not Android)"
        try:
            fine = check_permission(Permission.ACCESS_FINE_LOCATION)
            coarse = check_permission(Permission.ACCESS_COARSE_LOCATION)
            return f"Permission: fine={fine} coarse={coarse}"
        except Exception as exc:
            return f"Permission: query failed ({exc!r})"


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
        # The wire format carries param_value as float32, so quantize our
        # float64 intended value the same way before comparing; otherwise
        # large-magnitude floats fail the tolerance check spuriously.
        if not self._loading and name in self._modified:
            intended_f32 = struct.unpack(
                "f", struct.pack("f", self._modified[name]))[0]
            if abs(intended_f32 - value) < 1e-6:
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
            color=list(get_color("text_popup")),
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
    # NOTE: the literal defaults below duplicate the "dark" theme values
    # in app/theme.py, which is the single source of truth.  They exist
    # only so widgets have valid colors between KV load and the first
    # apply_theme() call; if a dark-theme color changes in theme.py,
    # update it here too.
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
    theme_text_button = ListProperty([1, 1, 1, 1])
    theme_tile_default = ListProperty([0.18, 0.18, 0.22, 1])
    theme_tile_border = ListProperty([0.3, 0.3, 0.35, 1])
    theme_status_error = ListProperty([0.7, 0.2, 0.2, 1])
    theme_param_modified_bg = ListProperty([0.3, 0.4, 0.2, 0.3])

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
        self.theme_text_button = list(get_color("text_button"))
        self.theme_tile_default = list(get_color("tile_default"))
        self.theme_tile_border = list(get_color("tile_border"))
        self.theme_status_error = list(get_color("status_error"))
        self.theme_param_modified_bg = list(get_color("param_modified_bg"))
        # Re-highlight the active nav button after theme change
        self._update_nav_buttons()

    def set_app_theme(self, name):
        """Switch theme, persist choice, and refresh UI."""
        set_theme(name)
        self.settings_data["theme"] = name
        _save_settings(self.settings_data)
        self.apply_theme()

    def build(self):
        # Settings presets (SoW 205195 §1.8): make sure the CS3.1 default
        # preset file exists (#24/#26), then resolve the working settings —
        # autosave, else legacy settings.json, else CS3.1 (#25/#26).
        _seed_cs31_preset()
        self.settings_data = _load_settings()
        # Snapshot of the selected preset file, for the saved/unsaved
        # indicator (#29).  None means "cannot match a file" -> unsaved.
        self.preset_snapshot = _load_preset_snapshot(
            self.settings_data.get("settings_preset", CS31_PRESET_NAME))
        # Establish the autosave placeholder immediately so a hard power-off
        # at any point after launch still finds a current file (#29).
        _save_settings(self.settings_data)

        # Ensure [program data]/Checklists exists with the default
        # pre-flight file whenever the folder is missing (SoW #15/#17).
        from gcs import checklists
        checklists.seed_default()

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

        # ── Compliance GPS logger (SoW #51) — TEMPORARY, remove before
        # production.  Toggled in Settings > Testing; deliberately NOT
        # persisted, so every launch starts with it OFF.  Starts/stops
        # automatically with the LIVE connection: the CONNECTION_CHANGED
        # event is also emitted by demo mode and replay, so the sync
        # handler gates on mav_client.running.
        self.compliance_gps_enabled = False
        self.compliance_logger = ComplianceGpsLogger()
        self._compliance_event = None  # Clock handle for the 2 s tick
        self.event_bus.subscribe(EventType.CONNECTION_CHANGED,
                                 lambda _data: self.sync_compliance_logger())

        # TEMPORARILY DISABLED: read-only battery-voltage web dashboard PoC.
        # A laptop joined to the Herelink hotspot previously browsed to
        # http://192.168.43.1:8000 for a live view.  Keep these lines here so
        # the PoC can be restored deliberately after it has been reviewed.
        # self.web_dashboard = WebDashboard(
        #     state_provider=self._dashboard_state
        # )
        # self.web_dashboard.start()

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

        # Restore the master transmit gate (SoW #49, Settings > Testing).
        # Persists in the autosave only (session key) so a Herelink reboot
        # mid-test-campaign keeps the gate where the tester left it.
        self.mav_client.tx_enabled = bool(
            self.settings_data.get("gcs_tx_enabled", True))
        if not self.mav_client.tx_enabled:
            log.warning("Starting with GCS MAVLink transmissions DISABLED "
                        "(Settings > Testing, SoW #49)")

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

    # ── Compliance GPS logger (SoW #51) — TEMPORARY, remove before
    # production ──────────────────────────────────────────────────────

    def sync_compliance_logger(self):
        """Start/stop the compliance GPS log from (toggle AND live link).

        Called from the Settings toggle and from CONNECTION_CHANGED
        events (which the event bus delivers on the main thread).  Gated
        on mav_client.running so demo mode and tlog replay — which emit
        the same event — never open a compliance log.  Logging runs for
        the whole connection regardless of armed state (per the
        presiding manager, a superset of #51's "while not armed").
        """
        want = self.compliance_gps_enabled and self.mav_client.running
        if want and self._compliance_event is None:
            self.compliance_logger.open()
            # 2 s period — the SoW ceiling is 4 s; half that absorbs
            # scheduling jitter.  First row immediately, not 2 s in.
            self._compliance_tick(0)
            self._compliance_event = Clock.schedule_interval(
                self._compliance_tick, 2.0)
        elif not want and self._compliance_event is not None:
            self._compliance_event.cancel()
            self._compliance_event = None
            self.compliance_logger.close()

    def _compliance_tick(self, _dt):
        """Write one row: controller + drone position (main thread)."""
        self.compliance_logger.log_row(
            ctrl_fix=self.device_location.last_fix,
            ctrl_alt=self.device_location.last_alt,
            drone_lat=self.vehicle_state.lat,
            drone_lon=self.vehicle_state.lon,
            drone_alt_amsl=self.vehicle_state.alt_amsl,
            drone_fix_type=self.vehicle_state.fix_type,
        )

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
        """Highlight the active nav button blue, reset others.

        Text color switches with the fill: text_button (white) on the
        saturated active highlight, text_title on the neutral inactive
        fill — required for readability in the high-contrast theme,
        where text_title is near-black.
        """
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
                    btn.color = self.theme_text_button
                else:
                    btn.background_color = self.theme_bg_input
                    btn.color = self.theme_text_title

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

    def _dashboard_state(self):
        """State snapshot for the web dashboard (PoC: liveness proof).

        Called from dashboard client threads — reads only.  Voltage is
        fed by whichever telemetry source is running (live, sim, or
        replay, since all three share self.vehicle_state), and the GCS
        clock ticks regardless, so the page visibly updates even with
        no vehicle connected.
        """
        return {
            "voltage": round(self.vehicle_state.voltage, 2),
            "connected": (self.mav_client.running
                          or self.sim.running
                          or self.replay_client.running),
            "msg_count": self.mav_client.msg_count,
            "gcs_time": datetime.datetime.now().strftime("%H:%M:%S"),
        }

    def on_stop(self):
        log.info("Application stopping – cleaning up…")
        if self.update_event:
            self.update_event.cancel()
        self.mav_client.stop()
        # CONNECTION_CHANGED dispatches via Clock, which may never run
        # another frame during shutdown — close the compliance log (SoW
        # #51, temporary) directly so the last rows are flushed.
        if self._compliance_event is not None:
            self._compliance_event.cancel()
            self._compliance_event = None
        self.compliance_logger.close()
        self.sim.stop()
        self.replay_client.stop()
        self.device_location.stop()
        # TEMPORARILY DISABLED with the battery-voltage web dashboard PoC.
        # self.web_dashboard.stop()


def main():
    CopterSondeGCSApp().run()


if __name__ == "__main__":
    main()
