"""
Data balancing for CopterSonde GCS (SoW section 1.1.2).

Turns the several variable-rate MAVLink streams into ONE stream of complete,
uniformly-timed samples — the foundation every downstream output (raw file,
ascent detection, altitude/time binning) is built on.

The component does exactly three things, per the SoW:
  1. Combine variable-frequency streams into one fixed-ish-frequency stream.
  2. Withhold incomplete lines — a line is emitted only once every required
     stream has contributed at least one message since the last line.
  3. Time-stamp every line in UNIX time, syncing the two boot-time datums
     (the autopilot, and the separately-booted CASS sensor head).

Required streams for one complete line (message id in parentheses):
    att   ATTITUDE (30)              roll/pitch/yaw + their rates
    gpos  GLOBAL_POSITION_INT (33)   ASL altitude, lat, lon
    lpos  LOCAL_POSITION_NED (32)    vx/vy/vz in m/s
    press SCALED_PRESSURE2 (137)     absolute pressure
    temp  CASS_SENSOR_RAW (227), app_datatype 0   three iMet temperatures
    rh    CASS_SENSOR_RAW (227), app_datatype 1   three HYT humidities
HEARTBEAT (0) is NOT balanced — its custom_mode and armed flag are stored and
carried forward onto each line (custom_mode drives ascent detection and the raw
file's mode column; the armed flag gates whether the raw file logs the line, per
the revised raw spec: SoW 205174 section 1.6 / 205192 section 5 log while armed).

Two choices here go beyond the SoW's single-box diagram; each is called out
in a comment where it lives and is summarised at the top so they're easy to
find and revisit:
  * CASS is one message multiplexed by app_datatype, so temperature and
    humidity are tracked as two SEPARATE required streams — a line waits for
    both, never just one.
  * Attitude angles are combined with a CIRCULAR mean, not an arithmetic one,
    so readings straddling the +/-pi wrap (routine when the aircraft points
    into a northerly wind) average correctly. Rates and everything else use a
    plain mean.

Units in BalancedLine are canonical/SI — identical to how VehicleState stores
them (K, %, m, deg, rad, rad/s, m/s, hPa). Display conversions (K->C, rad->deg,
hPa->mB label) happen later, at file-format time, not here.

Interface (self-contained — no MAVLink client or VehicleState coupling):
    line = balancer.feed(msg)   # returns a BalancedLine when one completes,
                                # else None
    balancer.reset()            # clear bins + time-sync; call on (re)connect
"""

import math
import time
from dataclasses import dataclass, field

from gcs.logutil import get_logger

log = get_logger("met_balancer")

# The streams that must all be present before a line is emitted.
_REQUIRED = frozenset({"att", "gpos", "lpos", "press", "temp", "rh"})

# HEARTBEAT.base_mode bit for "armed" (MAV_MODE_FLAG_SAFETY_ARMED).  Hard-coded
# here so the balancer needs no pymavlink import, the same way the ascent gate
# hard-codes AUTO_MODE / ASCENT_VZ.  The carried armed flag gates the raw file
# (SoW 205174 section 1.6 / 205192 section 5: log raw data while armed).
_ARMED_FLAG = 0x80   # 128


@dataclass
class BalancedLine:
    """One complete, uniformly-timed sample (canonical units; see module docstring)."""
    time: float = 0.0                                           # UNIX seconds
    alt_asl: float = 0.0                                        # m
    pressure: float = 0.0                                       # hPa (== mB)
    temps: list = field(default_factory=lambda: [0.0, 0.0, 0.0])      # K, A/B/C
    humidity: list = field(default_factory=lambda: [0.0, 0.0, 0.0])   # %, A/B/C
    lat: float = 0.0                                            # deg
    lon: float = 0.0                                            # deg
    roll: float = 0.0                                           # rad
    pitch: float = 0.0                                          # rad
    yaw: float = 0.0                                            # rad
    rollspeed: float = 0.0                                      # rad/s
    pitchspeed: float = 0.0                                     # rad/s
    yawspeed: float = 0.0                                       # rad/s
    vx: float = 0.0                                             # m/s north
    vy: float = 0.0                                             # m/s east
    vz: float = 0.0                                             # m/s down (+ = descending)
    custom_mode: int = 0                                        # carried from HEARTBEAT
    armed: bool = False                                        # carried from HEARTBEAT base_mode; gates the raw file
    satellites: int = 0                                        # GPS sats in view, carried from GPS_RAW_INT
    hdop: float = 99.99                                        # GPS HDOP (eph/100), carried; 99.99 = unknown


def _mean(xs):
    """Arithmetic mean. Only called for a stream that is present, so non-empty."""
    return sum(xs) / len(xs)


def _mean_columns(rows, n):
    """Per-column mean of ragged rows (each row is one message's channel array)."""
    out = []
    for col in range(n):
        vals = [r[col] for r in rows if col < len(r)]
        out.append(sum(vals) / len(vals) if vals else 0.0)
    return out


def _circular_mean(angles):
    """Mean of angles (radians) that respects the +/-pi wrap.

    Averaging 3.13 and -3.13 naively gives 0 (pointing forward); the circular
    mean gives ~pi (pointing back), which is correct.  Returns a value in
    [-pi, pi], matching MAVLink's attitude convention.
    """
    s = sum(math.sin(a) for a in angles)
    c = sum(math.cos(a) for a in angles)
    return math.atan2(s, c)


class MetBalancer:
    """Balances the live MAVLink streams into BalancedLine records.

    One instance per vehicle connection (reset() on connect).  It is a plain
    injected object rather than a module-level global — it carries per-flight
    state (the open bin and the two clock offsets), exactly like VehicleState
    and the file writers, and that keeps it unit-testable in isolation.

    Not thread-aware on purpose: feed() is driven from the single MAVLink IO
    thread, same as the existing per-message handlers.
    """

    def __init__(self, wall=time.time):
        # The wall clock is injectable so tests can drive time deterministically.
        self._wall = wall
        self.reset()

    def reset(self):
        """Clear the open bin, the time-sync offsets, and the carried mode.

        Called on every (re)connect: the autopilot may have rebooted, so the
        boot-ms datums must be re-synced from the next first message.
        """
        self._bin = {}            # stream key -> list of per-message value tuples
        self._offset = {}         # datum ("ap"/"cass") -> UNIX-minus-boot seconds
        self._custom_mode = 0     # latest HEARTBEAT.custom_mode, carried forward
        self._armed = False       # latest HEARTBEAT armed flag, carried forward
        self._satellites = 0      # latest GPS_RAW_INT.satellites_visible, carried forward
        self._hdop = 99.99        # latest GPS HDOP (eph/100), carried; 99.99 = unknown

    def feed(self, msg):
        """Feed one decoded MAVLink message.

        Returns a BalancedLine if this message completed a line, else None.
        Messages of unrelated types (and BAD_DATA) are ignored.
        """
        mtype = msg.get_type()

        # HEARTBEAT is stored, not balanced — carry its mode and armed flag
        # forward.  base_mode may be absent on some heartbeats; keep the last
        # known armed state if so (mirrors the custom_mode carry).
        if mtype == "HEARTBEAT":
            self._custom_mode = int(getattr(msg, "custom_mode", self._custom_mode))
            base_mode = getattr(msg, "base_mode", None)
            if base_mode is not None:
                self._armed = bool(base_mode & _ARMED_FLAG)
            return None

        # GPS_RAW_INT is GPS-receiver status (sats / HDOP): slowly varying
        # and lower-rate than the met streams, so it is carried forward like
        # the HEARTBEAT mode rather than balanced -- a met line is never
        # withheld waiting for it, and each line is stamped with the latest
        # fix.  The eph->HDOP conversion (and the 9999+ = unknown sentinel)
        # mirrors _on_gps_raw_int in mavlink_client; the two could be unified.
        if mtype == "GPS_RAW_INT":
            self._satellites = int(getattr(msg, "satellites_visible", self._satellites))
            eph = getattr(msg, "eph", None)
            self._hdop = eph / 100.0 if eph and eph < 9999 else 99.99
            return None

        # Map message -> (stream, datum, values in canonical units).
        if mtype == "ATTITUDE":
            stream, datum = "att", "ap"
            values = (msg.roll, msg.pitch, msg.yaw,
                      msg.rollspeed, msg.pitchspeed, msg.yawspeed)
        elif mtype == "GLOBAL_POSITION_INT":
            stream, datum = "gpos", "ap"
            values = (msg.alt / 1000.0, msg.lat / 1e7, msg.lon / 1e7)  # mm->m, degE7->deg
        elif mtype == "LOCAL_POSITION_NED":
            stream, datum = "lpos", "ap"
            values = (msg.vx, msg.vy, msg.vz)                          # already m/s
        elif mtype == "SCALED_PRESSURE2":
            stream, datum = "press", "ap"
            values = (msg.press_abs,)                                  # hPa == mB
        elif mtype == "CASS_SENSOR_RAW":
            # CASS is multiplexed: app_datatype selects the payload. Temperature
            # and humidity arrive as separate messages, so they are separate
            # required streams — a line waits for both.
            datum = "cass"
            dtype = getattr(msg, "app_datatype", None)
            vals = list(getattr(msg, "values", []) or [])
            if dtype == 0:                 # iMet temperatures (K)
                stream, values = "temp", vals[:3]
            elif dtype == 1:               # HYT humidities (%)
                stream, values = "rh", vals[:3]
            else:                          # resistance / wind / unknown — not balanced
                return None
        else:
            return None  # not a balanced stream (includes BAD_DATA)

        boot_s = getattr(msg, "time_boot_ms", 0) / 1000.0
        # Seed the boot->UNIX offset from the message's own receive timestamp
        # (msg._timestamp): live that is PC time at reception (unchanged), but on
        # a replayed .tlog it is the ORIGINAL recorded time, so the produced
        # timestamps match the flight rather than the replay.
        self._sync(datum, boot_s, getattr(msg, "_timestamp", None))

        self._bin.setdefault(stream, []).append(values)

        # Complete? Average everything, stamp from this (last-received) message.
        if _REQUIRED.issubset(self._bin):
            line = self._build_line(boot_s, datum)
            self._bin = {}
            return line
        return None

    def _sync(self, datum, boot_s, recv_time=None):
        """Record a datum's UNIX-minus-boot offset once, on its first message.

        ``recv_time`` is that message's own receive timestamp in UNIX seconds --
        PC time at reception for a live message, or the original recorded time
        for one replayed from a .tlog -- captured once and used to convert that
        datum's boot-ms stamps to UNIX thereafter.  When it is absent, fall back
        to the injected wall clock (PC time, assumed accurate per the SoW).
        """
        if datum not in self._offset:
            ref = recv_time if recv_time is not None else self._wall()
            self._offset[datum] = ref - boot_s

    def _build_line(self, trigger_boot_s, trigger_datum):
        """Average the current (complete) bin into one BalancedLine.

        Timestamp = the triggering message's boot time mapped through its own
        datum's offset (SoW: "timestamp of the last received message plus the
        offset obtained from synchronization").
        """
        b = self._bin
        att, gpos, lpos = b["att"], b["gpos"], b["lpos"]
        press, temp, rh = b["press"], b["temp"], b["rh"]
        return BalancedLine(
            time=trigger_boot_s + self._offset[trigger_datum],
            alt_asl=_mean([g[0] for g in gpos]),
            pressure=_mean([p[0] for p in press]),
            temps=_mean_columns(temp, 3),
            humidity=_mean_columns(rh, 3),
            lat=_mean([g[1] for g in gpos]),
            lon=_mean([g[2] for g in gpos]),
            roll=_circular_mean([a[0] for a in att]),
            pitch=_circular_mean([a[1] for a in att]),
            yaw=_circular_mean([a[2] for a in att]),
            rollspeed=_mean([a[3] for a in att]),
            pitchspeed=_mean([a[4] for a in att]),
            yawspeed=_mean([a[5] for a in att]),
            vx=_mean([l[0] for l in lpos]),
            vy=_mean([l[1] for l in lpos]),
            vz=_mean([l[2] for l in lpos]),
            custom_mode=self._custom_mode,
            armed=self._armed,
            satellites=self._satellites,
            hdop=self._hdop,
        )