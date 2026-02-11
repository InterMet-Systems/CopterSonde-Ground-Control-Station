# Porting Plan: BLISS-GCS (Windows) → CopterSonde GCS (Android/Kivy)

## Module Mapping

### Core Layer (gcs/)

| BLISS-GCS Windows Module | Android Module | Notes |
|---|---|---|
| `core/CopterSonde.py` (MavlinkMessage) | `gcs/mavlink_client.py` | Extend existing client with full vehicle state parsing |
| `core/data_manager.py` (DataManager) | `gcs/vehicle_state.py` (NEW) | Centralized vehicle state object; dew point calc; data arrays |
| `core/event_bus.py` (EventBus) | `gcs/event_bus.py` (NEW) | Kivy-adapted: use `Clock.schedule_once` for thread-safe UI callbacks |
| `core/CS_wind_estimate.py` | `gcs/wind_estimate.py` (NEW) | Port LESO wind estimation (numpy required) |
| `utils/audio_alert_manager.py` | `gcs/audio_manager.py` (NEW) | Android: use `kivy.core.audio` or SoundLoader; global mute toggle |

### UI Layer (app/screens/)

| BLISS-GCS Windows Window/Component | Android Screen | Key Widgets |
|---|---|---|
| `windows/connection_window.py` + `components/connection_panel.py` | `app/screens/connection.py` → `ConnectionScreen` | Transport selector (UDP/Serial), IP:port inputs, connect/disconnect, demo mode toggle, status indicator |
| `windows/telemetry_display_window.py` | `app/screens/telemetry.py` → `TelemetryScreen` | Grouped tiles: battery, GPS, radio, heading, speed, altitude, time-in-air. Threshold color-coding. Copy snapshot |
| Connection window ARM/Disarm/RTL + `windows/parameter_editor_window.py` | `app/screens/command.py` → `CommandScreen` | Arm/Disarm, mode select, Takeoff/Land/RTL, set parameter. Confirmation dialogs for safety-critical ops |
| `components/flight_hud.py` (UASHUD) | `app/screens/hud.py` → `HUDScreen` | Canvas-drawn attitude indicator, heading compass, altitude/speed tapes, vertical speed. 10-20 Hz refresh |
| `windows/sensor_data_window.py` (temp sensors + RH sensors over time) | `app/screens/sensor_plots.py` → `SensorPlotScreen` | T1/T2/T3 and RH1/RH2/RH3 vs time. Rolling window. Pause/resume. CSV export |
| `windows/met_data_window.py` (temp/dew/wind vs altitude profiles) | `app/screens/profile.py` → `ProfileScreen` | T vs alt, Td vs alt, wind speed/dir vs alt. Incremental profile builder. Clear profile |
| `windows/satellite_map_window.py` | `app/screens/map.py` → `MapScreen` | Tile-based map, drone marker + track, ADS-B markers. Toggles: ADS-B, track, center-on-drone |
| `windows/status_messages_window.py` + telemetry health tiles | `app/screens/monitoring.py` → `MonitoringScreen` | Link quality, GPS quality, sensor health, alert list, status message timeline |
| `windows/threshold_config_window.py` | `app/screens/settings.py` → `SettingsScreen` | Battery/GPS/temp/RH/wind thresholds. Persist JSON. Global mute toggle. Apply globally |

### Sim / Demo

| BLISS-GCS | Android | Notes |
|---|---|---|
| N/A (uses real connection) | `gcs/sim_telemetry.py` (NEW) | Generates moving GPS track, changing attitude/heading/altitude, temp/RH curves, wind, ADS-B tracks |

## Vehicle State Object

A single `VehicleState` dataclass in `gcs/vehicle_state.py`:

```
Fields:
  GPS: lat, lon, alt_amsl, alt_rel, fix_type, satellites, hdop
  Attitude: roll, pitch, yaw (radians)
  Heading: heading_deg
  Speed: groundspeed, airspeed, vx, vy, vz
  Battery: voltage, current, level
  Radio: rssi_percent
  System: armed, flight_mode, system_status, heartbeat_age
  Sensors: temperature_sensors[], humidity_sensors[], mean_temp, mean_rh, pressure
  Wind: wind_speed, wind_direction, vertical_wind_speed
  ADS-B: adsb_targets[]  (callsign, lat, lon, alt, heading, speed)
  Status Messages: status_messages[]
  Time: time_since_boot, utc_time
```

## Event Bus (Kivy-adapted)

Events emitted by `MAVLinkClient` / `SimTelemetry` from background thread:
- `DATA_UPDATED` — new telemetry sample available
- `CONNECTION_CHANGED` — connected/disconnected
- `ADSB_UPDATED` — ADS-B target list changed

UI screens subscribe and receive callbacks on the main thread via `Clock.schedule_once`.

## MAVLink Messages Used

| Message | Fields Extracted | Used By |
|---|---|---|
| HEARTBEAT | type, autopilot, base_mode (armed), custom_mode (flight mode) | Connection, Command, Telemetry |
| GLOBAL_POSITION_INT | lat, lon, alt, relative_alt, vx, vy, vz, hdg | GPS, Map, Profiles |
| ATTITUDE | roll, pitch, yaw | HUD |
| VFR_HUD | airspeed, groundspeed, heading, throttle, alt, climb | HUD, Telemetry |
| SYS_STATUS | voltage, current, battery_remaining | Telemetry |
| GPS_RAW_INT | fix_type, satellites_visible, eph | Telemetry, Monitoring |
| CASS_SENSOR_RAW (227) | temperature sensors, humidity sensors | Sensor Plots, Profiles |
| ARRC_SENSOR_RAW (228) | additional sensor data | Sensor Plots |
| STATUSTEXT | severity, text | Monitoring |
| COMMAND_ACK | command, result | Command |
| ADSB_VEHICLE | ICAO, lat, lon, alt, heading, speed, callsign | Map |
| SERVO_OUTPUT_RAW | servo1-8 raw | Wind estimation (RPM proxy) |
| RC_CHANNELS | rssi | Telemetry |

## Plotting Strategy (Android-compatible)

- Use `kivy.garden.matplotlib` or direct Kivy Canvas drawing for plots
- Kivy Canvas line/point drawing is lightweight and works on Android
- For profile plots (T vs altitude, wind vs altitude): Canvas-based custom widget
- For time series (sensor temps, RH): Canvas-based scrolling plot widget
- Cap data buffers (e.g., 3000 points max) to bound memory

## Implementation Order

1. **Connection Screen** — extends existing mavlink_client, adds UI for transport selection, demo mode
2. **Telemetry Display** — grouped tiles reading from VehicleState
3. **Command Screen** — ARM/Disarm/RTL/mode/takeoff with confirmations
4. **Flight HUD** — Canvas-drawn attitude indicator, compass, tapes
5. **CASS Sensor Plots** — T1/T2/T3 and RH vs time with rolling window
6. **Profile Screen** — T, Td, wind vs altitude profiles
7. **Satellite Map** — tile fetching, drone position, ADS-B overlay
8. **Monitoring Screen** — health dashboard, alert list, status messages
9. **Settings Screen** — threshold config, audio alerts, persist JSON

Each feature ends with a git commit.
