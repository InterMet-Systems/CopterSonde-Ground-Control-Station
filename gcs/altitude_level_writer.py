"""
Per-ascent Altitude-Level Message (ALM) writer for CopterSonde GCS.

The shared file mechanics -- constants block, filename / directory rules, the
open/write/close lifecycle, and the header+row column machinery -- live in
gcs.met_message_writer.MetMessageWriter.  This module supplies only what is ALM
specific: the Table 2-4 column list and its order, the altitude bin-center rule,
and the thin subclass binding them together.  See met_message_writer for the
file format (SoW 205192-11 section 2.2 / Tables 2-2, 2-3, 2-4).

ALM-specific column notes:
  * Altitude ASL reports the nominal 5 m bin center (see _bin_center) -- a
    client-accepted divergence from the SoW's bin-mean wording.
  * Latitude/Longitude report 6 dp per Table 2-4 (the Raw file uses 7).
  * The three time columns use the bin's mean time (the base default origin);
    Time Since Start is the .2f offset from the first data row.

NOT-YET-SOURCED FIELDS (placeholders):
  * The operator-input-file ground-station readings and home position, plus the
    SoW placeholders (powered/armed age, Data Quality), default to 0 / empty --
    see met_message_writer._constant_rows.  (Drone Serial Number now comes from
    the Remote ID settings.)
  * Satellites and HDOP flow from GPS_RAW_INT (msg 24) via the balancer and
    derive; only Data Quality remains a placeholder in the GPS/quality tail.
"""

import math

from gcs.met_derive import wind_speed_dir
from gcs.met_message_writer import Column, MetMessageWriter, _utc, _wind_dir_int

# Altitude-level bin width (SoW: 5 m).  Must match the alt binner width in
# mavlink_client (Binner(width=5.0, key=alt_asl)).
ALM_BIN_M = 5.0


def _bin_center(alt):
    """Nominal center of alt's altitude bin (...2.5 / ...7.5).

    Client-accepted divergence from the SoW: the ALM reports the bin center,
    not the data-weighted mean altitude the binner produces.  Bins align to
    whole multiples of ALM_BIN_M ([5k, 5k+5)) and the mean always lies inside
    its slice, so flooring it to the grid recovers the lower edge; add half a
    width for the center.  (Lives here, not in the binner, which is shared with
    the time-interval message whose altitude column wants the true mean.)
    """
    return math.floor(alt / ALM_BIN_M) * ALM_BIN_M + ALM_BIN_M / 2.0


# Data columns, in order (SoW Table 2-4).  LevelRecord carries canonical units
# (K, rad, rad/s, hPa, deg, m, m/s); getters apply the final display conversions
# (Kelvin->Celsius, radians->degrees) here.  Date/time getters return
# preformatted strings (fmt "{}").
ALM_COLUMNS = [
    Column("Altitude ASL",      "m",        "{:.1f}", lambda r, t0: _bin_center(r.alt_asl)),  # bin center, not mean (client divergence)
    Column("UTC Date",          "MM/DD/YY", "{}",     lambda r, t0: _utc(r.time).strftime("%m/%d/%y")),
    Column("UTC Time",          "HH:MM:SS", "{}",     lambda r, t0: _utc(r.time).strftime("%H:%M:%S")),
    Column("Time Since Start",  "s",        "{:.2f}", lambda r, t0: r.time - t0),
    Column("Pressure",          "mB",       "{:.2f}", lambda r, t0: r.pressure),
    Column("Air Temp",          "C",        "{:.2f}", lambda r, t0: r.temp - 273.15),
    Column("Rel Hum",           "%",        "{:.1f}", lambda r, t0: r.rh),
    Column("Wind Speed",        "m/s",      "{:.1f}", lambda r, t0: wind_speed_dir(r)[0]),
    Column("Wind Direction",    "deg",      "{:d}",   lambda r, t0: _wind_dir_int(r)),
    Column("Latitude",          "deg",      "{:.6f}", lambda r, t0: r.lat),
    Column("Longitude",         "deg",      "{:.6f}", lambda r, t0: r.lon),
    Column("Roll",              "deg",      "{:.1f}", lambda r, t0: math.degrees(r.roll)),
    Column("Roll Rate",         "deg/s",    "{:.1f}", lambda r, t0: math.degrees(r.rollspeed)),
    Column("Pitch",             "deg",      "{:.1f}", lambda r, t0: math.degrees(r.pitch)),
    Column("Pitch Rate",        "deg/s",    "{:.1f}", lambda r, t0: math.degrees(r.pitchspeed)),
    Column("Yaw",               "deg",      "{:.1f}", lambda r, t0: math.degrees(r.yaw)),
    Column("Yaw Rate",          "deg/s",    "{:.1f}", lambda r, t0: math.degrees(r.yawspeed)),
    Column("Ascent Rate",       "m/s",      "{:.1f}", lambda r, t0: r.ascent_rate),
    Column("Speed Over Ground", "m/s",      "{:.1f}", lambda r, t0: r.ground_speed),
    # Satellites/HDOP flow from GPS_RAW_INT via the balancer (carried) and
    # derive; satellites is int-rounded because the binner averages it to a
    # float.  Data Quality stays a SoW placeholder (no source yet).
    Column("Satellites",        "N/A",      "{:d}",   lambda r, t0: int(round(r.satellites))),
    Column("HDOP",              "N/A",      "{:.1f}", lambda r, t0: r.hdop),
    Column("Data Quality",      "N/A",      "{:d}",   lambda r, t0: 0),
]


class AltitudeLevelWriter(MetMessageWriter):
    """Writes one ALM .csv file per ascent (SoW 205192-11 section 2.2)."""

    PREFIX = "ALM"
    SUBDIR = "AltitudeLevel"
    COLUMNS = ALM_COLUMNS
    LOG_NAME = "alm_writer"
