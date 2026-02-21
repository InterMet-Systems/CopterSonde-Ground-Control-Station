# Project Structure

```
CopterSonde-Ground-Control-Station/
|
|-- main.py                         # Root entry point (thin wrapper for Buildozer)
|-- buildozer.spec                  # Buildozer config for Android APK packaging
|-- requirements.txt                # Python dependencies (kivy, pymavlink, certifi)
|-- settings.json                   # (generated) Persisted user settings
|-- .gitignore
|-- LICENSE
|-- README.md
|
|-- app/                            # Kivy UI layer
|   |-- __init__.py
|   |-- main.py                     # App class, all Screen classes, update loop
|   |-- app.kv                      # Kivy language layout for all screens + nav bar
|   |-- hud_widget.py               # FlightHUD canvas widget (attitude, heading, tapes)
|   |-- plot_widget.py              # TimeSeriesPlot & ProfilePlot canvas widgets
|   |-- map_widget.py               # MapWidget with satellite tile rendering & ADS-B
|   |-- tile_manager.py             # Slippy-map tile download, disk/memory cache, Mercator math
|   |-- theme.py                    # Theme color definitions (dark / high-contrast)
|
|-- gcs/                            # Core logic layer (no Kivy UI dependencies)
|   |-- __init__.py
|   |-- mavlink_client.py           # Threaded MAVLink UDP client, message parsing, commands
|   |-- vehicle_state.py            # Centralized vehicle telemetry state (deque history buffers)
|   |-- event_bus.py                # Thread-safe event bus (dispatches via Kivy Clock)
|   |-- sim_telemetry.py            # Simulated telemetry generator (demo mode)
|   |-- logutil.py                  # File + console logging (Android storage fallback)
|
|-- docs/                           # Documentation
|   |-- BUILD.md                    # Build and install instructions (desktop + Android)
|   |-- PORTING_PLAN.md             # Feature mapping from Windows BLISS-GCS
|   |-- STRUCTURE.md                # This file
|   |-- herelink_notes.md           # Herelink networking and MAVLink router research
|   |-- herelink_apk_installation.md # APK installation steps for Herelink
|
|-- scripts/                        # Helper scripts
|   |-- run_windows.bat             # Windows batch launcher
|   |-- run_windows.ps1             # Windows PowerShell launcher
|
|-- p4a-recipes/                    # Custom python-for-android recipes
|   |-- pymavlink/
|       |-- __init__.py             # pymavlink build recipe (skips lxml/fastcrc C deps)
|
|-- .github/workflows/              # CI/CD
|   |-- build.yml                   # Android APK + Windows executable builds
|
|-- .buildozer/                     # (generated) Buildozer build artifacts
```

## Layer Separation

The project separates **core logic** (`gcs/`) from **UI code** (`app/`):

- **`gcs/`** — Pure Python modules with no Kivy widget imports. These handle
  MAVLink communication, vehicle state management, event dispatch, and logging.
  They can be tested independently of the UI.

- **`app/`** — Kivy widgets, screens, and the main application class. These
  read from `VehicleState` and render telemetry data to the screen.

## Data Flow

```
MAVLink UDP ──> mavlink_client.py ──> VehicleState <── Screen.update(state)
(IO thread)    (parse + populate)    (shared obj)     (main thread, 10 Hz)
```

1. `mavlink_client.py` runs a background IO thread that receives MAVLink
   messages and writes parsed values into a `VehicleState` instance.
2. `app/main.py` runs a `Clock.schedule_interval` at 10 Hz that calls the
   current screen's `update(state)` method with the shared `VehicleState`.
3. Thread safety relies on Python's GIL — individual field assignments are
   atomic for built-in types, so no explicit locks are needed.

## Unified Flight Screen

The old Telemetry, Command, and HUD screens have been merged into a single
**FlightScreen** optimized for landscape tablets (Herelink controller):

```
+---------------------------+---------------------------+
|                           |                           |
|   TELEMETRY TABLE         |       FLIGHT HUD          |
|   (4 columns, scrollable) |   (attitude, heading,     |
|                           |    speed/altitude tapes)   |
|   MODE | ARMED | TIME |   |                           |
|   BATT | ...   | ...  |   +---------------------------+
|   ...  | ...   | ...  |   |       COMMANDS             |
|   ...  | ...   | ...  |   |                           |
|   ...                     |   [GENERATE MISSION]      |
|                           |   [CHECKLIST] [ARM&TKOFF] |
+---------------------------+   [LOITER]   [RTL]        |
|   STATUS MESSAGES         |   Feedback text           |
|   (severity-colored log)  |                           |
+---------------------------+---------------------------+
```

### Pre-Flight Checklist Gating

The ARM & TAKEOFF button is **disabled by default** and only becomes enabled
after the operator completes a pre-flight checklist popup. This enforces a
deliberate safety workflow:

1. **DISARMED state**: The PRE-FLIGHT CHECKLIST button is enabled; ARM &
   TAKEOFF is disabled.
2. Pressing PRE-FLIGHT CHECKLIST opens a popup with seven mandatory items
   (weather, battery, health, KP index, launch pad, mission, crew approval).
3. All seven checkboxes must be checked before the **Proceed** button enables.
4. After Proceed, the ARM & TAKEOFF button becomes enabled.
5. **ARMED state**: Both the checklist and arm buttons are disabled; the
   checklist popup auto-closes if open.
6. **Transition back to DISARMED**: The checklist resets — the operator must
   re-complete it before arming again.

## Performance Patterns

These patterns are used across the codebase to ensure smooth performance
on the Herelink v1.1 (Cortex-A53, 2 GB RAM, Android 7):

| Pattern | Where Used | Purpose |
|---------|-----------|---------|
| LRU text texture cache | `hud_widget.py`, `plot_widget.py`, `map_widget.py` | Avoids per-frame CoreLabel rasterization (most expensive ARM operation) |
| Dirty-flag + `Clock.schedule_once` | All canvas widgets | Coalesces multiple state updates into a single redraw per frame |
| `deque(maxlen=N)` | `vehicle_state.py` history buffers | O(1) append + eviction instead of O(n) list deletion |
| Track downsampling | `map_widget.py` | Stride-samples to 300 points max for rendering |
| Per-screen throttling | `app/main.py` `update_ui()` | Lower-priority screens refresh at 2-4 Hz instead of 10 Hz |
| `ThreadPoolExecutor(4)` | `tile_manager.py` | Bounds concurrent tile downloads to 4 threads |
| Binary search | `app/main.py` `SensorPlotScreen.update()` | O(log n) windowing for sensor time-series |
| Subscriber check | `mavlink_client.py` IO loop | Skips snapshot dict allocation when no listeners exist |

## Connection Presets

| Preset | Transport | IP | Port | Use Case |
|--------|-----------|-----|------|----------|
| HereLink Radio | `udpout` | `127.0.0.1` | `14552` | Herelink controller via internal MAVLink router |
| HereLink Hotspot | `udp` | `127.0.0.1` | `14550` | Herelink WiFi hotspot mode |
| SITL (mav-disabled) | `tcp` | `127.0.0.1` | `5760` | ArduPilot SITL without mavproxy |
| SITL (mav-enabled) | `udp` | `127.0.0.1` | `14560` | ArduPilot SITL with mavproxy |
| Custom | user-defined | user-defined | user-defined | Manual configuration |
