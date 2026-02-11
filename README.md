# CopterSonde Ground Control Station

A Kivy-based Ground Control Station (GCS) for ArduPilot CopterSonde
vehicles.  Designed to run on both Windows desktops and Android devices
(including Herelink controllers).

Uses a custom MAVLink dialect from
[tony2157/my-mavlink](https://github.com/tony2157/my-mavlink/tree/BLISS-ARRC-main)
that adds `CASS_SENSOR_RAW` (msg 227) and `ARRC_SENSOR_RAW` (msg 228).

## Features

| # | Feature | Screen | Status |
|---|---------|--------|--------|
| 1 | Connection management | Connect | Done |
| 2 | Telemetry data display | Telemetry | Done |
| 3 | Control and command | Command | Done |
| 4 | Flight HUD | HUD | Done |
| 5 | CASS temp + RH plots | Sensors | Done |
| 6 | Temp/dew/wind profiles | Profiles | Done |
| 7 | Satellite map + ADS-B | Map | Done |
| 8 | Tracking & monitoring | Monitor | Planned |
| 9 | Settings & alerts | Settings | Planned |

## Architecture

```
app/
  main.py            Kivy app with multi-screen navigation
  app.kv             KV layout for all screens + bottom nav bar
gcs/
  mavlink_client.py  Threaded MAVLink UDP client (full message parsing)
  vehicle_state.py   Centralized vehicle state object
  event_bus.py       Kivy-adapted thread-safe event bus
  sim_telemetry.py   Simulated telemetry generator for demo mode
  logutil.py         File-based logging utility
docs/
  BUILD.md           Build and install instructions
  PORTING_PLAN.md    Feature mapping from Windows app
  STRUCTURE.md       Project file layout
  herelink_notes.md  Herelink networking research
p4a-recipes/
  pymavlink/         Custom p4a recipe for Android build
scripts/
  run_windows.ps1    PowerShell launcher
  run_windows.bat    CMD launcher
buildozer.spec       Android packaging configuration
requirements.txt     Python dependencies (kivy, pymavlink)
main.py              Root entry point (used by Buildozer)
```

---

## Getting Started

See [docs/BUILD.md](docs/BUILD.md) for full build and run instructions.

### Quick start (desktop)

```bash
pip install -r requirements.txt
python app/main.py
```

### Demo mode

1. Launch the app
2. On the **Connect** screen, toggle **Demo Mode** ON
3. Simulated telemetry (GPS track, attitude, sensors, ADS-B) will populate
   all screens without needing a real vehicle

### Connecting to a vehicle

1. Enter the vehicle's IP address and UDP port (default 14550 desktop,
   14551 Herelink)
2. Tap **Connect**
3. Status indicator shows Healthy / No Heartbeat / Error

Connection settings are persisted automatically.

---

## Configuration

| Setting | File | Default | Description |
|---------|------|---------|-------------|
| `UDP_PORT` | `app/main.py` | `14551` (Android) / `14550` (desktop) | UDP listen port |
| `HEARTBEAT_TIMEOUT_S` | `gcs/mavlink_client.py` | `3.0` | Seconds before "unhealthy" |
| `GCS_SYSID` | `gcs/mavlink_client.py` | `255` | MAVLink system ID |
| `GCS_COMPID` | `gcs/mavlink_client.py` | `190` | MAVLink component ID |

## Parity Notes

Differences from the Windows BLISS-GCS reference app:

- **UI framework**: Kivy (touchscreen-optimized) instead of DearPyGui (desktop docking windows)
- **Navigation**: Bottom nav bar with dedicated screens instead of floating windows
- **Connection**: UDP only for now; serial support is platform-dependent on Android
- **Demo mode**: Built-in simulated telemetry for UI testing (not present in Windows app)

## License

MIT License — see [LICENSE](LICENSE).
