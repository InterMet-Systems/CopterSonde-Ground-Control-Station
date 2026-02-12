# Project Structure

```
CopterSonde-Ground-Control-Station/
|
|-- main.py                         # Entry point (launches the Kivy app)
|-- buildozer.spec                  # Buildozer config for Android packaging
|-- requirements.txt                # Python dependencies
|-- .gitignore
|-- LICENSE
|-- README.md
|
|-- app/                            # Kivy application layer
|   |-- __init__.py
|   |-- app.kv                      # Kivy UI layout file
|   |-- main.py                     # App class and screen definitions
|   |-- hud_widget.py               # FlightHUD canvas widget (attitude, heading, tapes)
|   |-- map_widget.py               # MapWidget with tile rendering & ADS-B overlay
|   |-- plot_widget.py              # TimeSeriesPlot & ProfilePlot canvas widgets
|   |-- theme.py                    # Theme color definitions (dark / high-contrast)
|
|-- gcs/                            # Ground Control Station core logic
|   |-- __init__.py
|   |-- event_bus.py                # Kivy-adapted thread-safe event bus
|   |-- logutil.py                  # Logging utilities
|   |-- mavlink_client.py           # MAVLink connection, parsing, commands
|   |-- sim_telemetry.py            # Simulated telemetry generator (demo mode)
|   |-- vehicle_state.py            # Centralized vehicle state dataclass
|
|-- docs/                           # Documentation
|   |-- BUILD.md                    # Build and install instructions
|   |-- PORTING_PLAN.md             # Feature mapping from Windows BLISS-GCS
|   |-- STRUCTURE.md                # This file
|   |-- herelink_notes.md           # Notes on Herelink integration
|
|-- scripts/                        # Helper scripts
|   |-- run_windows.bat             # Windows batch launcher
|   |-- run_windows.ps1             # Windows PowerShell launcher
|
|-- p4a-recipes/                    # Custom python-for-android recipes
|   |-- pymavlink/
|       |-- __init__.py             # pymavlink build recipe for p4a
|
|-- .github/workflows/              # CI/CD
|   |-- build.yml                   # Android APK + Windows executable builds
|
|-- .buildozer/                     # (generated) Buildozer build artifacts
```

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
|                           |   [LOITER]   [RTL]        |
|                           |   Status messages log     |
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
