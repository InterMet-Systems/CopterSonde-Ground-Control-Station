"""
fake_coptersonde.py — deterministic simulated CopterSonde for GCS testing.

Streams a hard-coded vertical-profile flight over MAVLink so the GCS can
be exercised end-to-end without a vehicle: connection, telemetry tiles,
HUD, map (incl. ADS-B), CASS sensor plots, profiles, status log, and —
the point of the exercise — tlog recording and replay (requirements #30
and #33).

Flight script (all times in seconds from launch of this program):
    0 - 8     on ground, disarmed, STABILIZE
    8         mode -> LOITER
    10        ARM ("Arming motors")
    12        mode -> AUTO
    13 - 53   climb at 3 m/s to 120 m
    53 - 63   hover at top of profile
    63        mode -> RTL
    63 - 103  descend at 3 m/s
    103 - 105 landed, still armed
    105       DISARM, mode -> STABILIZE
    105+      ground idle forever (Ctrl-C to stop)

Determinism: every field is a pure function of the tick index — no RNG —
so two runs produce identical message contents in identical order.  Only
the wall-clock pacing (and therefore the timestamps your TlogWriter
records) differs between runs, which is exactly what a tlog should show.

Usage:
    python fake_coptersonde.py [connection_string]

Default connection is udpout:127.0.0.1:14550, which pairs with the GCS
"HereLink Hotspot" preset (udp:127.0.0.1:14550) or a Custom
udpin/0.0.0.0/14550 connection.

CASS_SENSOR_RAW (msg 227) requires the client's custom pymavlink fork
(tony2157/my-mavlink: time_boot_ms, app_datatype, app_datalength,
values[5]).  On stock pymavlink the script still runs; it just warns once
and the Sensors/Profiles screens stay empty.
"""

import math
import os
import sys
import time

# MAVLink 2 framing, like HereLink/ArduPilot — must be set before import.
os.environ.setdefault("MAVLINK20", "1")

from pymavlink import mavutil  # noqa: E402

# Mirror the GCS: the custom fork embeds CASS messages in ardupilotmega;
# importing v20.all makes sure the dialect is registered if present.
try:
    import pymavlink.dialects.v20.all as _dialect  # noqa: F401,E402
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Configuration — the whole flight is defined by these constants
# ---------------------------------------------------------------------------
CONN_STR = sys.argv[1] if len(sys.argv) > 1 else "udpout:127.0.0.1:14550"
SYSID, COMPID = 1, 1          # pretend to be the autopilot
DT = 0.1                      # 10 Hz tick

# Launch site — Norman, OK (matches SimTelemetry for consistency)
BASE_LAT = 35.2226
BASE_LON = -97.4395
BASE_ALT_AMSL = 357.0         # m

TARGET_ALT = 120.0            # m AGL (matches the GCS mission default)
CLIMB_RATE = 3.0              # m/s
DESCENT_RATE = 3.0            # m/s

# Timeline (derived — change the constants above and it all shifts)
T_MODE_LOITER = 8.0
T_ARM = 10.0
T_MODE_AUTO = 12.0
T_LIFTOFF = 13.0
T_TOP = T_LIFTOFF + TARGET_ALT / CLIMB_RATE        # 53.0
T_HOVER_END = T_TOP + 10.0                          # 63.0  (mode -> RTL)
T_LAND = T_HOVER_END + TARGET_ALT / DESCENT_RATE    # 103.0
T_DISARM = T_LAND + 2.0                             # 105.0

# Wind coefficients — same SWX quadratic the GCS uses, so the pitch we
# send produces a self-consistent wind profile (~3.6 m/s at the surface
# rising to ~9.6 m/s at the top).
WS_A, WS_B = 37.1, 3.8

# Standard-atmosphere constants for the barometric formula (troposphere,
# valid far beyond our 477 m AMSL ceiling).  These let SCALED_PRESSURE2 fall
# off with height in lockstep with the altitude reported everywhere else, so
# the GCS's pressure and altitude readouts stay mutually consistent.
SEA_LEVEL_HPA = 1013.25       # standard sea-level pressure, hPa
SEA_LEVEL_TEMP_K = 288.15     # standard sea-level temperature, K
TEMP_LAPSE = 0.0065           # K/m
BARO_EXPONENT = 5.25588       # g0*M / (R*L), dimensionless

# ArduCopter custom mode numbers (used in HEARTBEAT.custom_mode)
COPTER_MODE = {"STABILIZE": 0, "AUTO": 3, "GUIDED": 4, "LOITER": 5,
               "RTL": 6, "LAND": 9}

_M = mavutil.mavlink
INFO, NOTICE, WARNING = (_M.MAV_SEVERITY_INFO, _M.MAV_SEVERITY_NOTICE,
                         _M.MAV_SEVERITY_WARNING)

# Scripted STATUSTEXT events: (time_s, severity, text)
EVENTS = [
    (1.0,   INFO,    "ArduCopter V4.5.7 (fake CopterSonde)"),
    (2.0,   INFO,    "Frame: QUAD/X"),
    (4.5,   INFO,    "EKF3 IMU0 is using GPS"),
    (9.0,   INFO,    "AutoVP: mission generated (120 m)"),
    (10.0,  NOTICE,  "Arming motors"),
    (12.0,  INFO,    "Mission: 2 WP"),
    (13.0,  INFO,    "AutoVP: profile start, target 120 m"),
    (33.0,  INFO,    "AutoVP: ascending through 60 m"),
    (53.0,  WARNING, "AutoVP: top of profile, wind 9.6 m/s"),
    (63.0,  INFO,    "AutoVP: descent started"),
    (103.5, NOTICE,  "Land complete"),
    (105.0, NOTICE,  "Disarming motors"),
    (107.0, INFO,    "Flight complete - idling on ground"),
]

SENSORS_OK = (_M.MAV_SYS_STATUS_SENSOR_3D_GYRO
              | _M.MAV_SYS_STATUS_SENSOR_3D_ACCEL
              | _M.MAV_SYS_STATUS_SENSOR_3D_MAG
              | _M.MAV_SYS_STATUS_SENSOR_ABSOLUTE_PRESSURE
              | _M.MAV_SYS_STATUS_SENSOR_GPS
              | _M.MAV_SYS_STATUS_SENSOR_RC_RECEIVER
              | _M.MAV_SYS_STATUS_SENSOR_BATTERY)

DEG_PER_M = 1.0 / 111320.0  # rough degrees latitude per metre


# ---------------------------------------------------------------------------
# Flight model — every function is a pure function of t (seconds)
# ---------------------------------------------------------------------------

def alt_rel(t):
    if t < T_LIFTOFF:
        return 0.0
    if t < T_TOP:
        return (t - T_LIFTOFF) * CLIMB_RATE
    if t < T_HOVER_END:
        return TARGET_ALT
    if t < T_LAND:
        return max(0.0, TARGET_ALT - (t - T_HOVER_END) * DESCENT_RATE)
    return 0.0


def vz_cms(t):
    """NED vertical speed, cm/s (positive = down)."""
    if T_LIFTOFF <= t < T_TOP:
        return -int(CLIMB_RATE * 100)
    if T_HOVER_END <= t < T_LAND:
        return int(DESCENT_RATE * 100)
    return 0


def armed(t):
    return T_ARM <= t < T_DISARM


def mode_name(t):
    if t < T_MODE_LOITER:
        return "STABILIZE"
    if t < T_MODE_AUTO:
        return "LOITER"
    if t < T_HOVER_END:
        return "AUTO"
    if t < T_DISARM:
        return "RTL"
    return "STABILIZE"


def phase(t):
    if t < T_ARM:
        return "preflight"
    if t < T_LIFTOFF:
        return "armed-ground"
    if t < T_TOP:
        return "climb"
    if t < T_HOVER_END:
        return "hover"
    if t < T_LAND:
        return "descend"
    if t < T_DISARM:
        return "landed-armed"
    return "postflight"


def swx_wind(pitch_rad):
    tan_p = math.tan(abs(pitch_rad))
    if tan_p <= 0:
        return 0.0
    return max(0.0, WS_A * tan_p + WS_B * math.sqrt(tan_p))


def baro_pressure(alt_amsl):
    """Absolute pressure (hPa) at a geometric altitude (m AMSL).

    Plain barometric formula for the troposphere.  Monotonic in altitude, so
    SCALED_PRESSURE2 follows the climb/descent profile exactly.
    """
    ratio = 1.0 - TEMP_LAPSE * alt_amsl / SEA_LEVEL_TEMP_K
    return SEA_LEVEL_HPA * ratio ** BARO_EXPONENT


def sample(t):
    """Compute the full vehicle state for time t."""
    z = alt_rel(t)
    ph = phase(t)
    flying = ph in ("climb", "hover", "descend")
    frac = z / TARGET_ALT

    # Attitude.  In flight, pitch grows with altitude so the SWX formula
    # yields an increasing wind profile; yaw veers ~25 deg over the climb.
    if flying:
        pitch_deg = 4.0 + 8.0 * frac + 1.2 * math.sin(2 * math.pi * t / 9.0)
        roll_deg = 1.5 * math.sin(t * 0.7)
        yaw_deg = 200.0 + 25.0 * frac + 3.0 * math.sin(t * 0.15)
    else:
        pitch_deg = 0.25 * math.sin(t * 0.5)
        roll_deg = 0.2 * math.sin(t * 0.6 + 1.0)
        yaw_deg = 197.0
    pitch = math.radians(pitch_deg)
    roll = math.radians(roll_deg)
    yaw = math.radians(((yaw_deg + 180.0) % 360.0) - 180.0)  # wrap to +-pi

    # Position: hover wander of a few metres while airborne
    if flying:
        north = 2.5 * math.cos(t / 13.0)
        east = 2.5 * math.sin(t / 11.0)
        vn = -(2.5 / 13.0) * math.sin(t / 13.0)
        ve = (2.5 / 11.0) * math.cos(t / 11.0)
    else:
        north = east = vn = ve = 0.0
    lat = BASE_LAT + north * DEG_PER_M
    lon = BASE_LON + east * DEG_PER_M / math.cos(math.radians(BASE_LAT))
    gs = math.hypot(vn, ve)

    wind = swx_wind(pitch) if flying else 0.0

    # Throttle / current by phase (deterministic ripple, no RNG)
    if ph == "climb":
        thr = 62 + int(3 * math.sin(t * 0.9))
        cur_ca = 2200 + int(150 * math.sin(t * 1.3))
    elif ph == "hover":
        thr = 48 + int(2 * math.sin(t * 1.1))
        cur_ca = 1600 + int(120 * math.sin(t * 1.1))
    elif ph == "descend":
        thr = 34 + int(2 * math.sin(t * 1.2))
        cur_ca = 950 + int(100 * math.sin(t * 1.2))
    elif ph in ("armed-ground", "landed-armed"):
        thr, cur_ca = 15, 350
    else:
        thr, cur_ca = 0, 60

    # Battery drains while armed, freezes after disarm
    te = max(0.0, min(t, T_DISARM) - T_ARM)
    voltage_mv = int((25.20 - 0.0065 * te) * 1000)
    batt_pct = max(0, 100 - int(0.16 * te))

    # Environment: 22 C at the surface, 8 C/km lapse; RH rises with height
    temp_c = 22.0 - 0.008 * z
    rh = 55.0 + 0.05 * z
    temps_k = [273.15 + temp_c + off + 0.12 * math.sin(t / 5.0 + i * 2.1)
               for i, off in enumerate((0.15, -0.08, 0.04))]
    rhs = [min(100.0, max(0.0, rh + off + 0.5 * math.sin(t / 6.0 + i * 1.7)))
           for i, off in enumerate((1.1, -0.7, 0.3))]

    # Secondary barometer (SCALED_PRESSURE2): pressure derived straight from
    # the AMSL altitude so it agrees with GLOBAL_POSITION_INT, plus a tiny
    # deterministic ripple so it reads like a live sensor instead of a perfect
    # analytic curve.  Its internal temperature sits a few degrees above
    # ambient, the way a board-mounted baro does from self-heating.
    alt_amsl = BASE_ALT_AMSL + z
    press_abs2 = baro_pressure(alt_amsl) + 0.05 * math.sin(t / 8.0)
    baro_temp_c = temp_c + 3.0 + 0.1 * math.sin(t / 7.0)

    return {
        "t": t, "phase": ph, "z": z, "armed": armed(t), "mode": mode_name(t),
        "roll": roll, "pitch": pitch, "yaw": yaw, "yaw_deg": yaw_deg % 360.0,
        "lat": lat, "lon": lon, "vn": vn, "ve": ve, "vz": vz_cms(t),
        "gs": gs, "airspeed": wind, "wind": wind,
        "throttle": thr, "current_ca": cur_ca,
        "voltage_mv": voltage_mv, "batt_pct": batt_pct,
        "sats": 14 + int(1.5 + 1.5 * math.sin(t / 19.0)),
        "eph": int(78 + 14 * math.sin(t / 13.0)),
        "rssi": int(185 + 28 * math.sin(t / 17.0)),
        "temps_k": temps_k, "rhs": rhs,
        "north": north, "east": east,
        "press_abs2": press_abs2, "baro_temp_c": baro_temp_c,
    }


# ---------------------------------------------------------------------------
# Senders
# ---------------------------------------------------------------------------

def make_cass_sender(conn):
    """Return send(boot_ms, dtype, vals) for CASS_SENSOR_RAW, or a no-op."""
    fn = getattr(conn.mav, "cass_sensor_raw_send", None)
    if fn is None:
        print("WARNING: CASS_SENSOR_RAW not in this pymavlink build — install")
        print("         the client's custom fork, or the GCS Sensors/Profiles")
        print("         screens will stay empty (everything else still works).")
        return lambda boot_ms, dtype, vals: None

    def send(boot_ms, dtype, vals):
        padded = ([float(v) for v in vals] + [0.0] * 5)[:5]  # values: float[5]
        fn(boot_ms, dtype, len(vals), padded)
    return send


def send_fast(conn, s, boot_ms):
    """10 Hz: ATTITUDE, GLOBAL_POSITION_INT, LOCAL_POSITION_NED, VFR_HUD."""
    conn.mav.attitude_send(
        boot_ms, s["roll"], s["pitch"], s["yaw"],
        0.02 * math.cos(s["t"] * 0.7), 0.02 * math.cos(s["t"] * 0.9), 0.0)
    conn.mav.global_position_int_send(
        boot_ms,
        int(round(s["lat"] * 1e7)), int(round(s["lon"] * 1e7)),
        int((BASE_ALT_AMSL + s["z"]) * 1000), int(s["z"] * 1000),
        int(s["vn"] * 100), int(s["ve"] * 100), s["vz"],
        int(round(s["yaw_deg"] * 100)) % 36000)

    # Same position and velocity as GLOBAL_POSITION_INT, expressed in the
    # local NED frame relative to the launch origin.  Down is the negative of
    # our AGL altitude, and vz comes straight from vz_cms() (already NED cm/s,
    # so /100 -> m/s).  x/y and vx/vy are a consistent kinematic pair, so a
    # client integrating the velocities tracks the reported position.  Sending
    # both keeps the GCS's global and local position readouts in agreement.
    conn.mav.local_position_ned_send(
        boot_ms,
        s["north"], s["east"], -s["z"],
        s["vn"], s["ve"], s["vz"] / 100.0)

    conn.mav.vfr_hud_send(
        s["airspeed"], s["gs"], int(s["yaw_deg"]), s["throttle"],
        BASE_ALT_AMSL + s["z"], -s["vz"] / 100.0)


def send_slow(conn, s, boot_ms):
    """1 Hz: heartbeat, system status, GPS, SCALED_PRESSURE2, RC, servos, time, ADS-B."""
    base_mode = _M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
    if s["armed"]:
        base_mode |= _M.MAV_MODE_FLAG_SAFETY_ARMED
    conn.mav.heartbeat_send(
        _M.MAV_TYPE_QUADROTOR, _M.MAV_AUTOPILOT_ARDUPILOTMEGA,
        base_mode, COPTER_MODE[s["mode"]],
        _M.MAV_STATE_ACTIVE if s["armed"] else _M.MAV_STATE_STANDBY)

    # NOTE: current_battery is in cA (10 mA units) per the MAVLink spec —
    # this is what a real ArduPilot sends.
    conn.mav.sys_status_send(
        SENSORS_OK, SENSORS_OK, SENSORS_OK, 350,
        s["voltage_mv"], s["current_ca"], s["batt_pct"],
        0, 0, 0, 0, 0, 0)

    conn.mav.gps_raw_int_send(
        boot_ms * 1000, 3,
        int(round(s["lat"] * 1e7)), int(round(s["lon"] * 1e7)),
        int((BASE_ALT_AMSL + s["z"]) * 1000),
        s["eph"], 110, int(s["gs"] * 100),
        int(round(s["yaw_deg"] * 100)) % 36000, s["sats"])

    # Secondary barometer.  press_diff stays 0 (this copter has no airspeed
    # sensor on the 2nd baro), so its differential-pressure temperature is
    # meaningless and we leave temperature_press_diff at its default — which
    # also keeps the call working on older pymavlink builds that predate that
    # field.  press_abs drops ~14 hPa over the 120 m climb and recovers on the
    # way down, so it traces the altitude profile.
    conn.mav.scaled_pressure2_send(
        boot_ms, s["press_abs2"], 0.0,
        int(round(s["baro_temp_c"] * 100)))

    chans = [1500, 1500, 1000 + 10 * s["throttle"], 1500,
             1800, 1000, 1100, 1500] + [0] * 10   # RC7 idle-low (AutoVP)
    conn.mav.rc_channels_send(boot_ms, 8, *chans, s["rssi"])

    base_pwm = 1000 + (9 * s["throttle"] if s["armed"] else 0)
    servos = [base_pwm + d for d in (5, -3, 2, -4, 6, -2, 3, -5)]
    conn.mav.servo_output_raw_send((boot_ms * 1000) % 2**32, 0, *servos)

    conn.mav.system_time_send(int(time.time() * 1e6), boot_ms)

    _send_adsb(conn, s["t"])


def _send_adsb(conn, t):
    """Two deterministic ADS-B contacts circling near the launch site."""
    flags = 31  # coords | altitude | heading | velocity | callsign valid
    a1 = t / 45.0
    conn.mav.adsb_vehicle_send(
        0xA404C5,
        int(round((BASE_LAT + 0.018 + 0.012 * math.cos(a1)) * 1e7)),
        int(round((BASE_LON + 0.010 + 0.014 * math.sin(a1)) * 1e7)),
        1, int(1850 * 1000), int((math.degrees(a1) + 90) % 360 * 100),
        5200, 0, b"N714CS", 1, 1, flags, 1200)
    a2 = 2.0 - t / 60.0
    conn.mav.adsb_vehicle_send(
        0xA404C6,
        int(round((BASE_LAT - 0.020 + 0.015 * math.cos(a2)) * 1e7)),
        int(round((BASE_LON - 0.005 + 0.018 * math.sin(a2)) * 1e7)),
        1, int(1250 * 1000), int((math.degrees(a2) - 90) % 360 * 100),
        4300, 0, b"SKW3712", 3, 1, flags, 1200)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    print(f"Fake CopterSonde -> {CONN_STR}  (MAVLink2, sysid {SYSID})")
    print("In the GCS, connect with the 'HereLink Hotspot' preset "
          "(udp:127.0.0.1:14550)\nor Custom udpin / 0.0.0.0 / 14550.  "
          "Ctrl-C to stop.\n")
    conn = mavutil.mavlink_connection(
        CONN_STR, source_system=SYSID, source_component=COMPID)
    send_cass = make_cass_sender(conn)

    n = 0
    event_idx = 0
    last_phase = None
    gcs_seen = False
    t_start = time.monotonic()

    while True:
        t = n * DT                      # deterministic: t derives from tick
        boot_ms = int(t * 1000)
        s = sample(t)

        if s["phase"] != last_phase:
            last_phase = s["phase"]
            print(f"[t={t:6.1f}s] phase: {s['phase']}  "
                  f"mode={s['mode']}  armed={s['armed']}")

        # Scripted status messages
        while event_idx < len(EVENTS) and EVENTS[event_idx][0] <= t:
            _t, sev, text = EVENTS[event_idx]
            conn.mav.statustext_send(sev, text.encode("ascii", "replace")[:50])
            event_idx += 1

        # 10 Hz core telemetry
        send_fast(conn, s, boot_ms)

        # 2 Hz CASS sensors (3 probes each; GCS appends history per message)
        if n % 5 == 0:
            send_cass(boot_ms, 0, s["temps_k"])   # iMet temperatures (K)
            send_cass(boot_ms, 1, s["rhs"])       # HYT humidity (%)

        # 1 Hz everything else (+ wind packet the GCS deliberately ignores)
        if n % 10 == 0:
            send_slow(conn, s, boot_ms)
            send_cass(boot_ms, 3, [s["yaw_deg"], s["wind"]])

        # Drain anything the GCS sends back (heartbeats, stream requests,
        # Remote ID, RC overrides) — acknowledged only by a one-time print.
        for _ in range(200):  # bounded drain per tick
            try:
                m = conn.recv_match(blocking=False)
            except OSError:
                # Windows quirk (WinError 10054): a UDP datagram sent to a
                # port with no listener bounces back as ICMP Port
                # Unreachable, which winsock reports on the NEXT recvfrom()
                # as ConnectionResetError.  It just means the GCS isn't
                # connected yet — the socket itself is fine.  Each failed
                # recvfrom consumes one queued error, so keep draining.
                # (The GCS's own _io_loop guards recv_match the same way.)
                continue
            if m is None:
                break
            if (not gcs_seen and m.get_type() == "HEARTBEAT"
                    and m.get_srcSystem() != SYSID):
                gcs_seen = True
                print(f"[t={t:6.1f}s] GCS link confirmed "
                      f"(sysid {m.get_srcSystem()})")

        # Absolute-schedule pacing: no drift, content stays tick-indexed
        n += 1
        delay = (t_start + n * DT) - time.monotonic()
        if delay > 0:
            time.sleep(delay)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")