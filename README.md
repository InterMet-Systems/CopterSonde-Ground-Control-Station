# CopterSonde Ground Control Station

A Kivy-based Ground Control Station (GCS) for ArduPilot CopterSonde
vehicles.  Designed to run on both Windows desktops and Android devices
(including Herelink v1.1 controllers).

Uses a custom MAVLink dialect from
[tony2157/my-mavlink](https://github.com/tony2157/my-mavlink/tree/BLISS-ARRC-main)
that adds `CASS_SENSOR_RAW` (msg 227) and `ARRC_SENSOR_RAW` (msg 228).

## Features

| # | Feature | Screen | Description |
|---|---------|--------|-------------|
| 1 | Connection management | Connect | Preset selector (HereLink Radio/Hotspot, SITL, Custom), demo mode, hold-to-disconnect safety |
| 2 | Telemetry + HUD + commands | Flight | Unified split-screen: telemetry tiles, flight HUD, mission generator, arm/takeoff, pre-flight checklist |
| 3 | CASS sensor plots | Sensors | T1/T2/T3 and RH1/RH2/RH3 time-series with pause, clear, CSV export |
| 4 | Atmospheric profiles | Profiles | Temperature, dew point, and wind speed vs altitude |
| 5 | Satellite map + ADS-B | Map | ArcGIS tile-based map with drone track, ADS-B targets, zoom/center/toggle controls |
| 6 | Parameter editor | Params | Read/search/edit/write ArduPilot parameters with pagination and diff highlighting |
| 7 | Settings & alerts | Settings | Alert thresholds, wind estimation coefficients, theme selection, data stream rate |

## Screens

```
 Connect   Flight    Map    Sensors   Profiles   Params   Settings
   |         |        |       |          |         |         |
   v         v        v       v          v         v         v
 [  Bottom navigation bar — tap to switch between screens  ]
```

**Flight Screen** combines telemetry, HUD, and commands in a single landscape layout:

```
+---------------------------+---------------------------+
|   TELEMETRY TABLE         |       FLIGHT HUD          |
|   (4-column tile grid)    |   (attitude, heading,     |
|                           |    speed/altitude tapes)   |
+---------------------------+---------------------------+
|   STATUS MESSAGES         |       COMMANDS             |
|   (severity-colored log)  |   [GENERATE] [CHECKLIST]  |
|                           |   [ARM&TKOFF] [LOITER]    |
|                           |   [RTL]                   |
+---------------------------+---------------------------+
```

## Architecture

```
app/                           # Kivy UI layer
  main.py                      App entry point, all Screen classes, update loop
  app.kv                       KV layout for all screens + bottom nav bar
  hud_widget.py                Canvas-drawn flight HUD (attitude, heading, tapes)
  plot_widget.py               TimeSeriesPlot & ProfilePlot canvas widgets
  map_widget.py                Satellite map with tile rendering & ADS-B overlay
  tile_manager.py              Slippy-map tile downloader, cache, and Mercator math
  theme.py                     Theme color definitions (dark / high-contrast)

gcs/                           # Core logic layer (no UI dependencies)
  mavlink_client.py            Threaded MAVLink UDP client, message parsing, commands
  vehicle_state.py             Centralized vehicle telemetry state object
  event_bus.py                 Thread-safe publish/subscribe with Kivy main-thread dispatch
  sim_telemetry.py             Simulated telemetry generator for demo mode
  logutil.py                   File + console logging (with Android storage fallback)

docs/                          # Documentation
  BUILD.md                     Build & run instructions (desktop, Android, PyInstaller)
  STRUCTURE.md                 Detailed project file layout
  PORTING_PLAN.md              Feature mapping from Windows BLISS-GCS
  herelink_notes.md            Herelink networking and MAVLink router notes
  herelink_apk_installation.md APK installation steps for Herelink

p4a-recipes/                   # Custom python-for-android recipes
  pymavlink/                   Build recipe for pymavlink with custom MAVLink dialect

scripts/                       # Helper scripts
  run_windows.ps1              PowerShell launcher
  run_windows.bat              CMD launcher

buildozer.spec                 Android packaging configuration
requirements.txt               Python dependencies (kivy, pymavlink, certifi)
main.py                        Root entry point (thin wrapper used by Buildozer)
```

## Data Flow

```
  MAVLink UDP  ──>  mavlink_client.py  ──>  VehicleState  <──  UI screens
  (background       (IO thread parses       (shared object      (main thread
   thread)           messages, updates       read by all         reads state
                     state fields)           screens)            at 10 Hz)
```

- The **IO thread** runs in `mavlink_client.py`, receives MAVLink messages, and
  writes parsed values directly into the `VehicleState` object.
- The **UI thread** runs the Kivy event loop.  A `Clock.schedule_interval` at
  10 Hz calls `update_ui()`, which delegates to the current screen's `update(state)`.
- **Thread safety** relies on Python's GIL for atomic reference assignment of
  built-in types — no explicit locks are needed between the IO and UI threads.

## Performance Optimizations

The app is optimized for the Herelink v1.1 controller (Cortex-A53, 2 GB RAM, Android 7):

- **Text texture LRU cache** — CoreLabel rasterization is cached in an OrderedDict,
  avoiding the most expensive per-frame operation on ARM.
- **Dirty-flag redraw coalescing** — Canvas widgets only redraw when data actually
  changes, and multiple updates per frame are coalesced into a single redraw.
- **`deque(maxlen)` history buffers** — O(1) append and eviction instead of O(n)
  `del list[0]` for all 14 telemetry history arrays.
- **Map track downsampling** — Flight track is stride-sampled to 300 points max
  before rendering, reducing per-frame trig operations by 10x.
- **Per-screen update throttling** — Flight/Sensors/Profiles update at 10 Hz;
  Map at ~4 Hz; Connection/Params/Settings at ~2 Hz.
- **Bounded tile download pool** — `ThreadPoolExecutor(max_workers=4)` instead of
  unbounded thread spawning for map tiles.
- **Binary search for plot windowing** — O(log n) time to find the visible data
  window in sensor time-series plots.

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

1. Select a connection preset from the dropdown:
   - **HereLink Radio** — `udpout:127.0.0.1:14552` (for Herelink controller)
   - **HereLink Hotspot** — `udp:127.0.0.1:14550`
   - **SITL (mav-disabled)** — `tcp:127.0.0.1:5760`
   - **SITL (mav-enabled)** — `udp:127.0.0.1:14560`
   - **Custom** — manually enter transport type, IP, and port
2. Tap **Connect**
3. Status indicator shows Healthy / No Heartbeat / Error

Connection settings are persisted automatically in `settings.json`.

---

## Configuration

| Setting | File | Default | Description |
|---------|------|---------|-------------|
| `DEFAULT_PORT` | `app/main.py` | `14552` (Android) / `14550` (desktop) | Default UDP port |
| `HEARTBEAT_TIMEOUT_S` | `gcs/mavlink_client.py` | `3.0` | Seconds before "unhealthy" |
| `GCS_SYSID` | `gcs/mavlink_client.py` | `255` | MAVLink system ID |
| `GCS_COMPID` | `gcs/mavlink_client.py` | `190` | MAVLink component ID |
| `UI_UPDATE_HZ` | `app/main.py` | `10` | Base UI refresh rate (Hz) |
| `stream_rate_hz` | Settings screen | `10` | MAVLink data stream request rate |

## Dependencies

| Package | Purpose |
|---------|---------|
| `kivy` >= 2.3.0 | Cross-platform UI framework (desktop + Android) |
| `pymavlink` >= 2.4.40 | MAVLink protocol library |
| `certifi` | CA certificate bundle for HTTPS tile downloads on Android |

## Parity Notes

Differences from the Windows BLISS-GCS reference app:

- **UI framework**: Kivy (touchscreen-optimized) instead of DearPyGui (desktop docking windows)
- **Navigation**: Bottom nav bar with dedicated screens instead of floating windows
- **Connection**: UDP/TCP only; serial support is platform-dependent on Android
- **Demo mode**: Built-in simulated telemetry for UI testing (not present in Windows app)
- **Parameter editor**: Paginated read/write with search and diff highlighting

## License

MIT License — see [LICENSE](LICENSE).
