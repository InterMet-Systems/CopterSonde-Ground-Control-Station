"""
Per-ascent Time-Interval Message (TIM) writer for CopterSonde GCS.

The shared file mechanics -- the constants block (identical to the ALM), the
filename / directory rules, the open/write/close lifecycle, and the header+row
column machinery -- live in gcs.met_message_writer.MetMessageWriter.  This
module supplies only what is TIM specific: the Table 2-6 column list and order,
the bin-start time rule, and the thin subclass binding them together.  See
met_message_writer for the file format (SoW 205192-11 section 2.3 / Tables 2-5,
2-3, 2-6).

How TIM differs from the ALM (same 22 fields, otherwise):
  * The three time columns lead (UTC Date, UTC Time, Time Since Start), then
    Altitude ASL at column 4.
  * Time is reported from the BEGINNING of each 1 s bin (SoW 2.3: "it will
    always be an integer number of seconds"), not the bin mean -- so the UTC
    date/time come from the bin's start second and Time Since Start is an
    integer.  See _bin_start.
  * Altitude ASL is the bin-mean altitude over the 1 s slice (TIM bins on time,
    not altitude, so the ALM's altitude bin-center rule does not apply here).
  * Filename prefix "TIM", written to Messages/TimeInterval.

Satellites/HDOP flow from GPS_RAW_INT just as in the ALM; Data Quality is still
a placeholder.
"""

import math

from gcs.met_derive import wind_speed_dir
from gcs.met_message_writer import Column, MetMessageWriter, _utc, _wind_dir_int


def _bin_start(t):
    """The integer UNIX second at the start (lower edge) of t's 1 s bin.

    The TIM reports time from the beginning of each 1 s bin (SoW 2.3), not the
    bin mean.  The time binner's grid is aligned to whole seconds and the
    binner-averaged time always lies inside its [k, k+1) slice, so flooring it
    recovers the bin's start second k.  (math.floor returns an int.)
    """
    return math.floor(t)


# Data columns, in order (SoW Table 2-6).  Columns 4-22 match the ALM's
# non-time columns exactly; only the leading time columns and their bin-start /
# integer handling differ.  LevelRecord carries canonical units; getters apply
# the final display conversions.  Date/time getters return preformatted strings.
TIM_COLUMNS = [
    Column("UTC Date",          "MM/DD/YY", "{}",     lambda r, t0: _utc(_bin_start(r.time)).strftime("%m/%d/%y")),
    Column("UTC Time",          "HH:MM:SS", "{}",     lambda r, t0: _utc(_bin_start(r.time)).strftime("%H:%M:%S")),
    Column("Time Since Start",  "s",        "{:d}",   lambda r, t0: _bin_start(r.time) - t0),
    Column("Altitude ASL",      "m",        "{:.1f}", lambda r, t0: r.alt_asl),   # bin mean (TIM bins on time)
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
    Column("Satellites",        "N/A",      "{:d}",   lambda r, t0: int(round(r.satellites))),
    Column("HDOP",              "N/A",      "{:.1f}", lambda r, t0: r.hdop),
    Column("Data Quality",      "N/A",      "{:d}",   lambda r, t0: 0),    # SoW placeholder
]


class TimeIntervalWriter(MetMessageWriter):
    """Writes one TIM .csv file per ascent (SoW 205192-11 section 2.3)."""

    PREFIX = "TIM"
    SUBDIR = "TimeInterval"
    COLUMNS = TIM_COLUMNS
    LOG_NAME = "tim_writer"

    def _origin(self, record):
        # Time Since Start is measured from the first bin's START second, not
        # the first bin's mean -- so every row's value is a whole-second integer.
        return _bin_start(record.time)
