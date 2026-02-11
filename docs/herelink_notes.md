# Herelink QGroundControl – MAVLink & Networking Notes

Summary of findings from the `qgroundcontrol-herelink` source code relevant
to building a custom GCS that works on a Herelink ground station.

---

## 1. UDP Port Configuration

### Default (upstream QGC)
- **Listen port:** `14550` (defined in `src/Settings/AutoConnect.SettingsGroup.json`, line 73)
- **Target host port:** `14550` (same file, line 85)
- **Target host IP:** empty by default (same file, line 79)

### Herelink override
The `HerelinkCorePlugin` (`custom/src/HerelinkCorePlugin.cc`, lines 58-66)
overrides these defaults at startup:

| Setting              | Herelink default | Upstream default |
|----------------------|------------------|------------------|
| `udpListenPort`      | **14551**        | 14550            |
| `udpTargetHostIP`    | **127.0.0.1**    | *(empty)*        |
| `udpTargetHostPort`  | **15552**        | 14550            |

The Herelink controller's internal radio bridge forwards vehicle telemetry
to the Android OS via localhost.  QGC listens on **14551** and sends
outbound MAVLink to **127.0.0.1:15552**, which the Herelink system service
relays over the radio link to the air unit.

> **Implication for CopterSonde GCS:** When running on a Herelink controller,
> bind to `udpin:0.0.0.0:14551` (or `udpin:127.0.0.1:14551`) to receive
> telemetry.  To send commands back, target `udpout:127.0.0.1:15552`.
> When using pymavlink's `udpin:` mode, replies are automatically routed
> back to the sender address, so binding to 14551 may be sufficient for
> bidirectional communication if the Herelink service sends from a known
> port.  Testing on hardware is needed to confirm.

### Where the port is consumed
`src/comm/UDPLink.cc` line 318-323 – `UDPConfiguration` constructor reads
`udpListenPort` and `udpTargetHostIP`/`udpTargetHostPort` from the
AutoConnect settings and uses them to bind the socket and optionally add a
target host.

---

## 2. Herelink-Specific IP Addresses

| IP Address         | Usage | Source |
|--------------------|-------|--------|
| `192.168.0.10`     | Herelink Air Unit RTSP video stream | `src/VideoManager/VideoManager.cc:753` |
| `192.168.43.1`     | Herelink Hotspot mode RTSP video stream | `src/VideoManager/VideoManager.cc:755` |
| `127.0.0.1`        | MAVLink target host for Herelink telemetry bridge | `custom/src/HerelinkCorePlugin.cc:63` |

The Herelink system uses a `192.168.0.x` subnet for the internal radio
link between the ground station and the air unit.  The air unit's RTSP
video endpoint is at `192.168.0.10:8554`.

When using Herelink in hotspot/tethering mode (sharing its connection to
another device), the video stream is available at `192.168.43.1:8554`.

---

## 3. Autoconnect & Connection Assumptions

The Herelink plugin **disables all non-UDP autoconnect types**
(`custom/src/HerelinkCorePlugin.cc`, lines 67-81):

- Pixhawk (USB serial) – disabled
- SiK Radio – disabled
- PX4 Flow – disabled
- RTK GPS – disabled
- LibrePilot – disabled
- NMEA – disabled
- ZeroConf – disabled

Only **UDP autoconnect** remains enabled.  The assumption is that the
Herelink controller exclusively communicates with the vehicle over its
built-in radio link, which presents as a UDP stream on localhost.

The autoconnect UDP link is named `"UDP Link (AutoConnect)"`
(`src/comm/LinkManager.cc:49`).

---

## 4. Video Streaming

Two Herelink-specific video sources are defined
(`src/Settings/VideoSettings.cc`, lines 32-33):

- **"Herelink AirUnit"** → `rtsp://192.168.0.10:8554/H264Video`
- **"Herelink Hotspot"** → `rtsp://192.168.43.1:8554/fpv_stream`

The Herelink build defaults the video source to "Herelink AirUnit"
(`custom/src/HerelinkCorePlugin.cc:87`).

A custom `VideoStreamControl` class (`custom/herelink/VideoStreamControl.cc`)
handles HDMI source switching on the air unit via MAVLink messages.

---

## 5. Android-Specific Details

- **Package name:** `org.cubepilot.herelink_qgroundcontrol` (`custom/custom.pri:45`)
- **APK artifact:** `QGroundControl-Herelink.apk` (`.github/workflows/android_release.yml:37`)
- **Joystick:** The Herelink controller's built-in sticks appear as a
  joystick device named `"gpio-keys"` (`custom/src/HerelinkCorePlugin.cc:101`).
  The plugin auto-selects this joystick and enables it when a vehicle connects.
- **Font size:** Defaults to 10pt for the Herelink's small screen
  (`custom/src/HerelinkCorePlugin.cc:44`).
- **UI scaling:** Several QML components check `QGroundControl.corePlugin.isHerelink`
  to trigger `scaleForSmallScreen` behavior (e.g., `FlyViewInstrumentPanel.qml:24`).
- **Palette:** Defaults to the "outdoor" dark palette
  (`custom/src/HerelinkCorePlugin.cc:51`).

---

## 6. Key Takeaways for CopterSonde GCS

1. **Standard port 14550** works for non-Herelink setups (SITL, telemetry
   radios, companion computers).
2. **On Herelink,** listen on port **14551** (primary) and target
   **127.0.0.1:15552** for outbound MAVLink.  Port **14552** is a
   secondary "eavesdropping" endpoint — use it only if you need to run
   *alongside* the stock QGC app (which already owns 14551).
3. The connection is **always UDP** on Herelink – no serial.
4. All non-UDP autoconnect methods are irrelevant on Herelink.
5. Video is available via RTSP at `192.168.0.10:8554` (air unit) or
   `192.168.43.1:8554` (hotspot mode) – useful for future video features.
