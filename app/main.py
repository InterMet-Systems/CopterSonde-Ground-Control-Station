"""
CopterSonde Ground Control Station – Kivy application entry point.

Run with:
    python app/main.py

On Android (via Buildozer / python-for-android) the same file is
packaged as the application main module.
"""

import os
import sys

# ---------------------------------------------------------------------------
# Ensure the repo root is on sys.path so ``gcs.*`` imports work regardless
# of how the script is launched.
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ---------------------------------------------------------------------------
# Kivy configuration – must come BEFORE any other kivy import
# ---------------------------------------------------------------------------
from kivy.config import Config  # noqa: E402

# Landscape window on desktop (Android orientation is set in buildozer.spec)
Config.set("graphics", "width", "960")
Config.set("graphics", "height", "540")
Config.set("graphics", "resizable", "1")

from kivy.app import App  # noqa: E402
from kivy.clock import Clock  # noqa: E402
from kivy.lang import Builder  # noqa: E402
from kivy.metrics import dp, sp  # noqa: E402, F401
from kivy.uix.boxlayout import BoxLayout  # noqa: E402

from gcs.logutil import setup_logging, get_logger  # noqa: E402
from gcs.mavlink_client import MAVLinkClient  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Herelink's mavlink-router forwards vehicle telemetry to localhost:14551
# (the primary port used by QGC).  On desktop/SITL the standard port is 14550.
try:
    import android  # noqa: F401 – only available on Android/p4a
    UDP_PORT = 14551
except ImportError:
    UDP_PORT = 14550

UI_UPDATE_HZ = 4          # how often the UI polls MAVLink state

setup_logging()
log = get_logger("app")

# Load the .kv file relative to this script
_KV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.kv")
Builder.load_file(_KV_PATH)


class GCSRoot(BoxLayout):
    """Root widget – defined in app.kv."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._mav = MAVLinkClient(port=UDP_PORT)
        self._update_event = None

    # ── Button handler ────────────────────────────────────────────────
    def on_connect_toggle(self):
        if self._mav.running:
            self._disconnect()
        else:
            self._connect()

    # ── Connection lifecycle ──────────────────────────────────────────
    def _connect(self):
        try:
            self._mav.start()
        except Exception as exc:
            log.error("Connection failed: %s", exc)
            self.ids.status_label.text = "Connection Error"
            self.ids.status_label.color = (0.9, 0.4, 0.1, 1)
            self.ids.detail_label.text = str(exc)
            return

        self.ids.connect_btn.text = "Disconnect"
        self.ids.connect_btn.background_color = (0.6, 0.2, 0.2, 1)

        # Schedule periodic UI refresh
        self._update_event = Clock.schedule_interval(
            self._update_ui, 1.0 / UI_UPDATE_HZ
        )
        log.info("Connected – UI update scheduled at %d Hz", UI_UPDATE_HZ)

    def _disconnect(self):
        if self._update_event is not None:
            self._update_event.cancel()
            self._update_event = None

        self._mav.stop()

        self.ids.connect_btn.text = "Connect"
        self.ids.connect_btn.background_color = (0.2, 0.55, 0.3, 1)
        self.ids.status_label.text = "Not Connected"
        self.ids.status_label.color = (0.7, 0.2, 0.2, 1)
        self.ids.detail_label.text = "Disconnected"
        log.info("Disconnected")

    # ── Periodic UI refresh (runs on Kivy main thread) ────────────────
    def _update_ui(self, _dt):
        mav = self._mav
        if not mav.running:
            return

        age = mav.heartbeat_age()

        if mav.is_healthy():
            self.ids.status_label.text = "Healthy"
            self.ids.status_label.color = (0.15, 0.75, 0.3, 1)
            self.ids.detail_label.text = (
                f"Heartbeat age: {age:.1f} s\n"
                f"Vehicle sysid={mav.last_sysid}  compid={mav.last_compid}\n"
                f"MAV_TYPE={mav.vehicle_type}  AUTOPILOT={mav.autopilot_type}"
            )
        elif mav.last_sysid is not None:
            self.ids.status_label.text = "No Heartbeat"
            self.ids.status_label.color = (0.9, 0.6, 0.1, 1)
            self.ids.detail_label.text = (
                f"Last heartbeat: {age:.1f} s ago\n"
                f"Last seen sysid={mav.last_sysid}  compid={mav.last_compid}"
            )
        else:
            self.ids.status_label.text = "No Heartbeat"
            self.ids.status_label.color = (0.7, 0.2, 0.2, 1)
            self.ids.detail_label.text = (
                f"Listening on UDP port {mav.port} …\n"
                "No vehicle heartbeat received yet."
            )

    # ── Cleanup ───────────────────────────────────────────────────────
    def cleanup(self):
        """Called when the app is stopping."""
        self._disconnect()


class CopterSondeGCSApp(App):
    """Kivy Application class."""

    title = "CopterSonde GCS"

    def build(self):
        self._root = GCSRoot()
        return self._root

    def on_pause(self):
        # Android: allow the app to be paused without killing it.
        return True

    def on_resume(self):
        # Android: nothing special needed on resume – the IO thread
        # keeps running (or can be restarted).
        pass

    def on_stop(self):
        log.info("Application stopping – cleaning up MAVLink …")
        if self._root is not None:
            self._root.cleanup()


def main():
    CopterSondeGCSApp().run()


if __name__ == "__main__":
    main()
