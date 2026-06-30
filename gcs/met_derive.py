"""
Per-sample derivation for the altitude-level message (SoW section 1.5 /
Table 1-1).

derive() turns one BalancedLine into one LevelRecord -- the per-sample shape of
the altitude-level data, before any binning.  It is pure and per-sample: the
three derived quantities (wind, ascent rate, ground speed) and the probe
averages are computed here, once per sample, and the 5 m altitude binning and
field averaging happen downstream in the binner.

Three deliberate choices (settled with the customer):
  * Wind uses the CGCS pitch-only fit, NOT the SoW rotation-matrix version --
    wind_h = max(0, ws_a*tan|pitch| + ws_b*sqrt(tan|pitch|)), with direction
    taken as vehicle yaw (the CopterSonde points into the wind).  The fit lives
    here in wind_speed(); _compute_wind in mavlink_client calls this same
    function with the same live coefficients, so the live readout and the
    message files share one formula and can never drift.
  * Wind is computed per sample and then averaged (not computed once from the
    bin-averaged attitude); the two differ because the fit is non-linear.
  * Wind is carried as east/north vector components so the binner can average
    it linearly and recover speed/direction from the bin-mean vector -- which
    is the correct way to average a direction.  wind_speed_dir() is the inverse.

Pass-through fields keep BalancedLine's canonical units (K, rad, rad/s, hPa,
deg, m, m/s); the Kelvin->Celsius and radians->degrees conversions happen in the
level writer's column getters at emit, exactly as the raw writer does.
"""

import math
from dataclasses import dataclass

from gcs.met_balancer import BalancedLine

# Canonical default CGCS pitch-only wind-fit coefficients -- the SINGLE source
# of truth for the fit.  The MAVLink client seeds its mutable, user-tunable
# self.ws_a / self.ws_b from these at construction; every wind calculation (the
# live readout via _compute_wind AND the ALM/TIM/WMO files via derive) then runs
# through wind_speed() with those live values, so the two paths can never use a
# different formula or drift apart.
WS_A = 37.1
WS_B = 3.8

# LevelRecord fields that are angles on a circle (radians): the binner must
# average these with a circular mean (atan2 of summed sin/cos), never a plain
# arithmetic mean, or anything near the +/-pi wrap is corrupted.  Wind is
# averaged as a vector (wind_e / wind_n) instead, so it is NOT listed here.
CIRCULAR_FIELDS = ("roll", "pitch", "yaw")


@dataclass
class LevelRecord:
    """One per-sample row of altitude-level data (SoW Table 1-1), pre-binning.

    Wind is stored as east/north vector components rather than speed/direction
    so the binner can average it linearly; use wind_speed_dir() to recover the
    Wind Speed / Wind Direction columns from the (bin-mean) vector.
    """
    time: float = 0.0          # s, UNIX
    alt_asl: float = 0.0       # m (ASL)
    pressure: float = 0.0      # hPa == mB
    temp: float = 0.0          # K, mean of the three probes
    rh: float = 0.0            # %, mean of the three probes
    wind_e: float = 0.0        # m/s, wind vector east component
    wind_n: float = 0.0        # m/s, wind vector north component
    lat: float = 0.0           # deg
    lon: float = 0.0           # deg
    roll: float = 0.0          # rad   [circular]
    rollspeed: float = 0.0     # rad/s
    pitch: float = 0.0         # rad   [circular]
    pitchspeed: float = 0.0    # rad/s
    yaw: float = 0.0           # rad   [circular]
    yawspeed: float = 0.0      # rad/s
    ascent_rate: float = 0.0   # m/s, = -vz
    ground_speed: float = 0.0  # m/s, = sqrt(vx^2 + vy^2)
    satellites: float = 0.0    # GPS sats in view (int on the wire; bin mean here)
    hdop: float = 99.99        # GPS HDOP (eph/100); 99.99 = unknown


def _mean(values):
    """Arithmetic mean of a non-empty sequence."""
    return sum(values) / len(values)


def wind_speed(pitch, ws_a, ws_b):
    """CGCS pitch-only wind speed (m/s) from pitch (radians) and the fit
    coefficients.

    wind_h = max(0, ws_a*tan|p| + ws_b*sqrt(tan|p|)), and 0 when there is no
    tilt.  This is the one wind formula in the system: mavlink_client's
    _compute_wind (the live readout) and derive() (the ALM/TIM/WMO files) both
    call it with the client's live ws_a / ws_b, so the two can never diverge.
    """
    tan_p = math.tan(abs(pitch))
    if tan_p > 0:
        return max(0.0, ws_a * tan_p + ws_b * math.sqrt(tan_p))
    return 0.0


def wind_speed_dir(rec):
    """Recover (wind speed m/s, wind direction deg in [0, 360)) from a record's
    wind vector -- the inverse of derive()'s decomposition, in the level
    message's units.

    The level writer uses this for the Wind Speed / Wind Direction columns; the
    binner averages wind_e / wind_n first, so in the pipeline this runs on the
    bin-mean vector (the magnitude of the mean vector, not the mean magnitude).
    """
    speed = math.hypot(rec.wind_e, rec.wind_n)
    direction = math.degrees(math.atan2(rec.wind_e, rec.wind_n)) % 360.0
    return speed, direction


def derive(line: BalancedLine, ws_a, ws_b) -> LevelRecord:
    """Compute one per-sample LevelRecord from one BalancedLine.

    ``ws_a`` / ``ws_b`` are the live wind-fit coefficients -- the client passes
    its user-tunable self.ws_a / self.ws_b -- so a Settings change propagates
    into the derived wind (and thus the ALM/TIM/WMO files) here.
    """
    speed = wind_speed(line.pitch, ws_a, ws_b)
    direction = line.yaw                       # radians; vehicle points into wind
    return LevelRecord(
        time=line.time,
        alt_asl=line.alt_asl,
        pressure=line.pressure,
        temp=_mean(line.temps),                # collapse 3 probes -> 1 (K)
        rh=_mean(line.humidity),               # collapse 3 probes -> 1 (%)
        wind_e=speed * math.sin(direction),    # vector, for linear bin-averaging
        wind_n=speed * math.cos(direction),
        lat=line.lat,
        lon=line.lon,
        roll=line.roll,
        rollspeed=line.rollspeed,
        pitch=line.pitch,
        pitchspeed=line.pitchspeed,
        yaw=line.yaw,
        yawspeed=line.yawspeed,
        ascent_rate=-line.vz,                  # vz is down-positive
        ground_speed=math.hypot(line.vx, line.vy),
        satellites=line.satellites,            # GPS status, carried (see met_balancer)
        hdop=line.hdop,
    )
