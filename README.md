# CopterSonde Ground Control Station

A lightweight, Kivy-based Ground Control Station (GCS) for ArduPilot
vehicles.  Designed to run on both Windows desktops and Android devices
(including Herelink controllers).

Uses a custom MAVLink dialect from
[tony2157/my-mavlink](https://github.com/tony2157/my-mavlink/tree/BLISS-ARRC-main)
that adds `CASS_SENSOR_RAW` (msg 227) and `ARRC_SENSOR_RAW` (msg 228).

## Features (MVP)

- MAVLink heartbeat monitoring with live link-health display
- GCS heartbeat transmission at 1 Hz (`MAV_TYPE_GCS`)
- UDP transport (auto-selects port 14551 on Herelink, 14550 on desktop)
- Non-blocking background MAVLink I/O thread
- Android pause/resume aware
- Landscape-optimized touchscreen UI

## Project Structure

```
app/
  main.py            Kivy application entry point
  app.kv             UI layout (KV language)
gcs/
  mavlink_client.py  Threaded MAVLink UDP client
  logutil.py         File-based logging utility
docs/
  BUILD.md           Extended build notes and emulator setup
  herelink_notes.md  Herelink networking research
p4a-recipes/
  pymavlink/         Custom p4a recipe (builds pymavlink with BLISS/ARRC messages)
scripts/
  run_windows.ps1    PowerShell launcher (creates venv automatically)
  run_windows.bat    CMD launcher (creates venv automatically)
buildozer.spec       Android packaging configuration
requirements.txt     Python dependencies (kivy, pymavlink)
main.py              Root entry point (used by Buildozer on Android)
```

---

## Getting Started

See [docs/BUILD.md](docs/BUILD.md) for full build and run instructions covering:

- **Windows** — install prerequisites, create a venv, and run the app
- **Android** — cross-compile a debug APK with Buildozer
- **Android Emulator** — test on a virtual device via Android Studio
- **Herelink** — automatic port detection (no manual config needed)

For detailed Herelink networking research (ports, IPs, video streams), see
[docs/herelink_notes.md](docs/herelink_notes.md).

---

## Configuration

Key settings are defined as constants near the top of each module:

| Setting | File | Default | Description |
|---------|------|---------|-------------|
| `UDP_PORT` | `app/main.py` | `14551` (Android) / `14550` (desktop) | UDP listen port (auto-detected) |
| `HEARTBEAT_TIMEOUT_S` | `gcs/mavlink_client.py` | `3.0` | Seconds without heartbeat before "unhealthy" |
| `GCS_HEARTBEAT_INTERVAL_S` | `gcs/mavlink_client.py` | `1.0` | GCS heartbeat transmit interval |
| `GCS_SYSID` | `gcs/mavlink_client.py` | `255` | MAVLink system ID for this GCS |
| `GCS_COMPID` | `gcs/mavlink_client.py` | `190` | MAVLink component ID for this GCS |

## License

MIT License — see [LICENSE](LICENSE).
