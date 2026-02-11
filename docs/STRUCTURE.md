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
|   |-- logutil.py                  # Logging utilities
|   |-- mavlink_client.py           # MAVLink connection and communication
|
|-- docs/                           # Documentation
|   |-- BUILD.md                    # Build and install instructions
|   |-- herelink_notes.md           # Notes on Herelink integration
|   |-- STRUCTURE.md                # This file
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
