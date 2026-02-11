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
|-- .buildozer/                     # (generated) Buildozer build artifacts
```
