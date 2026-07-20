"""
Minimal pure-Python netCDF3 (classic) file writer.

WHY THIS EXISTS -- the WMO_UAS_A writer originally used scipy.io.netcdf_file,
which needs numpy + scipy.  Neither ships in the Herelink APK (buildozer's
``requirements`` line), and the p4a scipy recipe drags in a Fortran/LAPACK
toolchain for what is, here, a few hundred bytes of output.  The netCDF3
classic format itself is tiny and fully specified (NetCDF Classic Format
Specification, Unidata), so this module writes it directly with ``struct``:
stdlib only, importable and functional on the Herelink, on Windows, anywhere.

SCOPE -- deliberately just what the WMO_UAS_A file needs:
  * one fixed dimension,
  * 1-D float32 ("f") / float64 ("d") variables on that dimension,
  * text attributes (global and per-variable).
No record dimension, no other types.  Extend it if a future file needs more.

The header layout (order of entries, name/value padding to 4-byte boundaries,
big-endian encoding, vsize rounding) follows the spec exactly as scipy's writer
does, so the output is byte-identical to what scipy.io.netcdf_file produced for
the same data -- verified in tests against files from the previous
implementation.
"""

import struct

# Format tags from the netCDF classic spec.
_MAGIC = b"CDF\x01"        # classic format, 32-bit offsets
_ABSENT = struct.pack(">ii", 0, 0)
_NC_DIMENSION = 10
_NC_VARIABLE = 11
_NC_ATTRIBUTE = 12
_NC_CHAR = 2

# Supported variable typecodes: netCDF external type id and byte size.
_NC_TYPE = {"f": 5, "d": 6}          # NC_FLOAT, NC_DOUBLE
_ITEM_SIZE = {"f": 4, "d": 8}
_PACK_FMT = {"f": "f", "d": "d"}


def _pad4(b):
    """Pad bytes to the next 4-byte boundary with NULs (spec: header and data
    entities are 4-byte aligned)."""
    return b + b"\x00" * ((4 - len(b) % 4) % 4)


def _name(s):
    """Encode a name: length (int32) + characters, NUL-padded to 4 bytes."""
    b = s.encode("ascii")
    return struct.pack(">i", len(b)) + _pad4(b)


def _text_attr(name, value):
    """Encode one text attribute: name, NC_CHAR, nelems, padded characters."""
    v = value.encode("ascii")
    return _name(name) + struct.pack(">ii", _NC_CHAR, len(v)) + _pad4(v)


def _attr_list(attrs):
    """Encode an attribute list (or ABSENT when empty)."""
    if not attrs:
        return _ABSENT
    out = struct.pack(">ii", _NC_ATTRIBUTE, len(attrs))
    for key, value in attrs:
        out += _text_attr(key, value)
    return out


def write_netcdf3(path, global_attrs, dim_name, dim_len, variables):
    """Write a netCDF3-classic file with one fixed dimension.

    ``global_attrs``  -- [(name, str value), ...], written in order.
    ``dim_name``      -- name of the single dimension (e.g. "obs").
    ``dim_len``       -- its length (number of observations).
    ``variables``     -- [(name, typecode, [(attr, str value), ...], values), ...]
                         where typecode is "f" (float32) or "d" (float64) and
                         ``values`` is a sequence of dim_len Python floats.
    """
    # --- variable metadata entries, minus the 'begin' offset (filled below) ---
    var_entries = []      # header bytes for each variable, up to and incl. vsize
    var_data = []         # each variable's packed, padded data bytes
    for name, typecode, attrs, values in variables:
        values = list(values)
        if len(values) != dim_len:
            raise ValueError("variable %r has %d values, dimension %r is %d"
                             % (name, len(values), dim_name, dim_len))
        nc_type = _NC_TYPE[typecode]                # KeyError = unsupported type
        vsize = len(values) * _ITEM_SIZE[typecode]
        vsize += (4 - vsize % 4) % 4                # spec: vsize incl. padding
        entry = _name(name)
        entry += struct.pack(">ii", 1, 0)           # ndims=1, dimid[0]=0
        entry += _attr_list(attrs)
        entry += struct.pack(">ii", nc_type, vsize)
        var_entries.append(entry)
        data = struct.pack(">%d%s" % (len(values), _PACK_FMT[typecode]), *values)
        var_data.append(_pad4(data))

    # --- header size, so each variable's data 'begin' offset is known ---
    header = _MAGIC
    header += struct.pack(">i", 0)                  # numrecs (no record dim)
    header += struct.pack(">ii", _NC_DIMENSION, 1)  # dim_list, 1 dimension
    header += _name(dim_name) + struct.pack(">i", dim_len)
    header += _attr_list(global_attrs)              # gatt_list
    if var_entries:
        header += struct.pack(">ii", _NC_VARIABLE, len(var_entries))
    else:
        header += _ABSENT
    header_len = len(header) + sum(len(e) + 4 for e in var_entries)  # +4: begin

    # --- assemble: header (with begins) then each variable's data block ---
    begin = header_len
    body = b""
    for entry, data in zip(var_entries, var_data):
        header += entry + struct.pack(">i", begin)  # begin: int32 in CDF-1
        body += data
        begin += len(data)

    with open(path, "wb") as f:
        f.write(header)
        f.write(body)
