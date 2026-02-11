# Build & Run Instructions

## Windows Development

### Prerequisites

- Python 3.10+ (from python.org or Microsoft Store)
- Git

### Quick Start (standard pymavlink from PyPI)

```powershell
# Clone and enter the repo
cd CopterSonde-Ground-Control-Station

# Create virtual environment and install dependencies
python -m venv .venv
.venv\Scripts\Activate.ps1          # PowerShell
# or: .venv\Scripts\activate.bat    # CMD

pip install -r requirements.txt

# Run the app
python app\main.py
```

Or use the helper scripts:

```powershell
.\scripts\run_windows.ps1      # PowerShell
# or
scripts\run_windows.bat        # CMD
```

### Quick Start (custom BLISS/ARRC dialect)

To get the custom MAVLink messages (`CASS_SENSOR_RAW`, `ARRC_SENSOR_RAW`)
available on desktop, build pymavlink from the custom definitions repo:

```powershell
# In a separate directory (outside this project):
git clone -b BLISS-ARRC-main https://github.com/tony2157/my-mavlink.git
cd my-mavlink
git submodule update --init pymavlink

# On Windows (PowerShell):
cd pymavlink
$env:MDEF = "..\message_definitions"
pip install .

# On Linux / WSL:
cd pymavlink
MDEF=../message_definitions pip install .
```

Then return to this project and run `pip install -r requirements.txt` to
install kivy (pymavlink is already installed from the step above).

### Testing MAVLink

To send test heartbeats to the GCS on your Windows machine you can use
SITL (ArduPilot Software-In-The-Loop) or a simple pymavlink script:

```python
# test_heartbeat_sender.py
from pymavlink import mavutil
import time

conn = mavutil.mavlink_connection("udpout:127.0.0.1:14550")
while True:
    conn.mav.heartbeat_send(
        mavutil.mavlink.MAV_TYPE_QUADROTOR,
        mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
        0, 0,
        mavutil.mavlink.MAV_STATE_STANDBY,
    )
    print("heartbeat sent")
    time.sleep(1)
```

---

## Android Build (Ubuntu 22.04+ / WSL recommended)

### 1. System packages

```bash
sudo apt update
sudo apt install -y \
    python3 python3-pip python3-venv \
    git zip unzip openjdk-17-jdk \
    autoconf libtool pkg-config \
    zlib1g-dev libncurses-dev \
    cmake libffi-dev libssl-dev \
    build-essential libltdl-dev \
    adb
```

### 2. Install Buildozer and Cython

```bash
python3 -m venv ~/buildozer-venv
source ~/buildozer-venv/bin/activate

pip install --upgrade pip setuptools wheel
pip install buildozer cython
```

### 3. Build the debug APK

```bash
cd /mnt/c/Users/<you>/Github/CopterSonde-Ground-Control-Station  # WSL path
source ~/buildozer-venv/bin/activate

# First build — downloads Android SDK/NDK (~10-20 min on a fast connection).
buildozer -v android debug
```

The custom `p4a-recipes/pymavlink/` recipe handles:
- Downloading pymavlink 2.4.49 from PyPI (runtime code)
- Downloading the custom BLISS/ARRC MAVLink XML definitions from GitHub
- Regenerating the dialect modules with `CASS_SENSOR_RAW` and
  `ARRC_SENSOR_RAW` via `MDEF`
- Skipping lxml, fastcrc, and C extensions (not needed at runtime)

The APK is written to:

```
bin/coptersondeGCS-0.1.0-arm64-v8a-debug.apk
```

### 4. Clean build (if changing dependencies)

When you change `requirements` in `buildozer.spec` or modify the p4a
recipe, clean first so the build graph is regenerated:

```bash
buildozer android clean
buildozer -v android debug
```

### 5. Install on device / emulator

```bash
# With a device connected via USB (enable USB debugging first):
adb install -r bin/coptersondeGCS-0.1.0-arm64-v8a-debug.apk

# Launch:
adb shell am start -n com.intermetsystems.coptersondeGCS/org.kivy.android.PythonActivity
```

### 6. View logs

```bash
adb logcat -s python:D
```

---

## Android Studio Emulator (optional)

If you want to test on an emulator instead of a physical device:

1. **Install Android Studio** from https://developer.android.com/studio.
2. Open **Device Manager** (Tools > Device Manager).
3. Create a virtual device:
   - Choose a phone profile (e.g., Pixel 6).
   - Select a system image with API 24+ (e.g., API 33, arm64-v8a or x86_64).
   - Finish the wizard and start the emulator.
4. Verify the emulator is visible to ADB:
   ```bash
   adb devices
   # Should show something like: emulator-5554  device
   ```
5. Install the APK:
   ```bash
   adb -s emulator-5554 install -r bin/coptersondeGCS-0.1.0-arm64-v8a-debug.apk
   ```
   > **Note:** If the emulator runs an x86_64 image, you will need to rebuild
   > with `android.archs = x86_64` in `buildozer.spec`.

6. Forward a UDP port from the host to the emulator so you can send test
   heartbeats from the host:
   ```bash
   adb -s emulator-5554 reverse udp:14550 udp:14550
   ```
   (Or use `adb forward` depending on direction.)

---

## Herelink Controller

The app **automatically detects** when it is running on Android and switches
to the correct Herelink UDP port.  No manual configuration is needed.

| Environment | Listen port | How it's set |
|-------------|-------------|--------------|
| Desktop / SITL | **14550** | Default (no `android` module) |
| Herelink (Android) | **14551** | Auto-detected via `import android` |

Port 14551 is the primary MAVLink telemetry port used by the Herelink's
internal `mavlink-router` service — the same port the stock QGC app uses.
Outbound commands are routed back through the mavlink-router automatically
via pymavlink's `udpin:` reply path.

> **Note:** The stock QGC app must be disabled on the Herelink for this app
> to bind port 14551.  Disable it with:
> ```bash
> adb shell pm disable-user --user 0 org.mavlink.qgroundcntrol
> ```

See [herelink_notes.md](herelink_notes.md) for details on Herelink
networking discovered from the qgroundcontrol-herelink source.
