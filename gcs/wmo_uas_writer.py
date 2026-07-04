"""
Per-ascent WMO_UAS_A message writer for CopterSonde GCS (SoW 205174 section 1.7).

This message carries the same altitude-binned data as the ALM, re-expressed as a
CF/WMO-conformant netCDF file for the WMO UAS Data Collection pipeline.  It is
therefore fed the SAME ``alm_bin`` records the ALM writer receives (the
processing flowchart forks the altitude-level structure to both the ASCII file
and the netCDF file), with WMO units/types (Table 1-5) and one extra derived
variable, the humidity mixing ratio (section 1.7.1).

FORMAT CHOICE -- netCDF3 (classic), written with scipy.  The target GCS now runs
on an Android HereLink controller, where the HDF5 stack behind netCDF4 is
impractical to build; scipy's netCDF3 writer needs only numpy/scipy and produces
a valid CF file.  (The numpy/scipy imports are deferred into close() so the rest
of the app -- and the other writers -- don't depend on them at import time.)

WRITE MODEL -- unlike the streaming ASCII writers, a netCDF3 file is written as a
whole dataset.  Records are accumulated during the ascent and the file is written
once, at close() (ascent end, or stop() on a mid-ascent disconnect).  A hard
crash mid-ascent would lose that ascent's .nc, but the same data is on disk in
the streamed ALM/TIM/Raw files, so it is recoverable.

FILENAME -- the WMO UASDC convention (source 1):
``UASDC_<operatorID>_<airframeID>_<YYYYMMDDHHMMSS>Z.nc`` with a literal trailing
Z (Zulu).  Per section 1.7 the timestamp is the FIRST DATA LINE's time (the first
emitted bin) -- note this is a hair later than the ALM/TIM filenames, which use
the ascent's first raw point.  operatorID/airframeID are the section-18 operator
inputs; until that plumbing exists they default to the placeholders below.
"""

import math
import os
import threading

from gcs.logutil import get_logger
from gcs.met_derive import wind_speed_dir
from gcs.met_message_writer import _utc
from gcs.storage_paths import mirror_file, output_dirs


def _mixing_ratio(record):
    """Humidity mixing ratio [kg/kg] from RH/T/P (SoW 205174 section 1.7.1).

    Ps via the Magnus form (6.112 hPa coefficient, T in C); P stays in hPa so the
    pressure units cancel in the ratio, and U is RH as a decimal.
    """
    t_c = record.temp - 273.15
    u = record.rh / 100.0
    ps = 6.112 * math.exp(17.62 * t_c / (243.12 + t_c))   # sat. vapor pressure [hPa]
    return 0.6219743 * u * ps / (record.pressure - ps)


class WmoUasWriter:
    """Writes one WMO_UAS_A netCDF file per ascent (SoW 205174 section 1.7)."""

    SUBDIR = "WMO_UAS_A"

    # Section-18 operator inputs.  begin() takes the real operator ID / drone
    # serial from the Remote ID settings; these are the fallback when unset.
    OPERATOR_ID = "0"
    AIRFRAME_ID = "0"

    # Global attributes (Table 1-4), in order.  String literals, case-sensitive.
    # (wmo__cf_profile reads "FM 303-2024" -- the WMO code-form designator FM 303,
    # 2024 edition -- spanning the table's Dimension/Value split.)
    GLOBAL_ATTRS = [
        ("Conventions",      "CF-1.8, WMO-CF-1.0"),
        ("wmo__cf_profile",  "FM 303-2024"),
        ("featureType",      "trajectory"),
        ("platform_name",    "CopterSonde 3"),
        ("processing_level", "c1"),
    ]

    # Data variables (Table 1-5), in order:
    #   name, nc type ("f"=Float/f4, "d"=Double/f8), standard_name, long_name,
    #   units, axis (None where the table says n/a), getter(record)->value.
    # Values come straight from the altitude-binned record in WMO units: altitude
    # is the bin MEAN (not the ALM's center snap), pressure hPa->Pa, temperature
    # stays K, wind direction is the unrounded float, lat/lon are doubles.
    VARIABLES = [
        ("altitude",                         "f", "Altitude",          "Altitude (height)",     "m ASL",                              "Z",  lambda r: r.alt_asl),
        ("time",                             "d", "Time",              "Time",                  "seconds since 1970-01-01T00:00:00",  "T",  lambda r: r.time),
        ("air_pressure",                     "f", "Air Pressure",      "Air Pressure",          "Pa",                                 "Z",  lambda r: r.pressure * 100.0),
        ("air_temperature",                  "f", "Air Temperature",   "Air Temperature",       "K",                                  None, lambda r: r.temp),
        ("relative_humidity",                "f", "Relative Humidity", "Relative Humidity",     "%",                                  None, lambda r: r.rh),
        ("wind_speed",                       "f", "Wind Speed",        "Wind Speed",            "m/s",                                None, lambda r: wind_speed_dir(r)[0]),
        ("wind_direction",                   "f", "Wind Direction",    "Wind Direction",        "deg",                                None, lambda r: wind_speed_dir(r)[1] % 360.0),
        ("lat",                              "d", "Latitude",          "Latitude",              "deg",                                "Y",  lambda r: r.lat),
        ("lon",                              "d", "Longitude",         "Longitude",             "deg",                                "X",  lambda r: r.lon),
        ("platform_roll",                    "f", "Roll",              "Roll",                  "deg",                                None, lambda r: math.degrees(r.roll)),
        ("platform_roll_rate",               "f", "Roll Rate",         "Roll Rate",             "deg/s",                              None, lambda r: math.degrees(r.rollspeed)),
        ("platform_pitch",                   "f", "Pitch",             "Pitch",                 "deg",                                None, lambda r: math.degrees(r.pitch)),
        ("platform_pitch_rate",              "f", "Pitch Rate",        "Pitch Rate",            "deg/s",                              None, lambda r: math.degrees(r.pitchspeed)),
        ("platform_yaw",                     "f", "Yaw",               "Yaw",                   "deg",                                None, lambda r: math.degrees(r.yaw)),
        ("platform_yaw_rate",                "f", "Yaw Rate",          "Yaw Rate",              "deg/s",                              None, lambda r: math.degrees(r.yawspeed)),
        ("platform_speed_wrt_ground",        "f", "Speed Over Ground", "Speed Over Ground",     "m/s",                                None, lambda r: r.ground_speed),
        ("platform_speed_wrt_ground_upward", "f", "Ascent Rate",       "Ascent Rate",           "m/s",                                None, lambda r: r.ascent_rate),
        ("humidity_mixing_ratio",            "f", "Mixing Ratio",      "Humidity Mixing Ratio", "kg/kg",                              None, _mixing_ratio),
    ]

    _NP_DTYPE = {"f": "f4", "d": "f8"}   # nc typecode -> numpy dtype

    def __init__(self, log_dir=None, backup_dir=None):
        if log_dir is None:
            log_dir, backup_dir = self._default_dirs()
        self._dir = log_dir
        self._backup_dir = backup_dir
        self._lock = threading.Lock()
        self._log = get_logger("wmo_writer")
        self._records = []           # alm_bin records accumulated this ascent
        self._first_time = None      # first data line's time (-> filename stamp)
        self._operator_id = self.OPERATOR_ID
        self._airframe_id = self.AIRFRAME_ID
        self._path = None            # path of the last file written

    @property
    def path(self):
        return self._path

    def _default_dirs(self):
        # (primary, backup) Messages/<SUBDIR> -- same shared-base resolution
        # the ASCII writers use (mirrors MetMessageWriter._default_dirs).
        return output_dirs("Messages", self.SUBDIR)

    def begin(self, start_time=None, raw_path=None, operator_id=None, airframe_id=None):
        """Start a fresh ascent's accumulation.

        ``start_time`` and ``raw_path`` are accepted for parity with the ASCII
        writers (so the client opens all writers uniformly) but are unused here:
        the netCDF filename's timestamp comes from the first data line, and
        netCDF carries no raw-filename field.  ``operator_id``/``airframe_id``
        are the operator ID / drone serial from the Remote ID settings; each
        falls back to "0" when empty.
        """
        with self._lock:
            self._records = []
            self._first_time = None
            self._operator_id = operator_id or self.OPERATOR_ID
            self._airframe_id = airframe_id or self.AIRFRAME_ID

    def write_row(self, record):
        """Accumulate one altitude-binned record (the same record the ALM gets)."""
        with self._lock:
            if self._first_time is None:
                self._first_time = record.time
            self._records.append(record)

    def close(self):
        """Write this ascent's accumulated records to a netCDF file, then reset.

        No-op when nothing was accumulated (an ascent that ended before any bin
        completed produces no file).  numpy/scipy are imported here so the module
        stays importable -- and the other writers usable -- without them.
        """
        with self._lock:
            records = self._records
            if not records:
                self._records = []
                self._first_time = None
                return
            try:
                import numpy as np
                from scipy.io import netcdf_file

                os.makedirs(self._dir, exist_ok=True)
                stamp = _utc(self._first_time).strftime("%Y%m%d%H%M%S")
                base = "UASDC_{}_{}_{}Z".format(self._operator_id, self._airframe_id, stamp)
                path = os.path.join(self._dir, base + ".nc")
                n = 1                                   # deconflict identical names
                while os.path.exists(path):
                    path = os.path.join(self._dir, "{}_{}.nc".format(base, n))
                    n += 1

                ds = netcdf_file(path, "w")
                try:
                    for attr, value in self.GLOBAL_ATTRS:
                        setattr(ds, attr, value)
                    ds.createDimension("obs", len(records))
                    for name, nctype, std, lng, units, axis, get in self.VARIABLES:
                        col = np.array([get(r) for r in records], dtype=self._NP_DTYPE[nctype])
                        var = ds.createVariable(name, nctype, ("obs",))
                        var.standard_name = std
                        var.long_name = lng
                        var.units = units
                        if axis is not None:
                            var.axis = axis
                        var[:] = col
                    ds.flush()
                finally:
                    ds.close()
                self._path = path
                self._log.info("WMO_UAS_A file written: %s (%d obs)", path, len(records))
                # One-shot writer: mirror the finished file (SoW #3).
                mirror_file(path, self._backup_dir)
            except Exception:
                self._log.exception("Failed to write WMO_UAS_A file")
            finally:
                self._records = []
                self._first_time = None
