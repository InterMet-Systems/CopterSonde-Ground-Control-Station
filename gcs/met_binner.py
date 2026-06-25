"""
Generic binner for the altitude-level and time-interval messages.

A Binner accumulates per-sample LevelRecords into fixed-width intervals and
emits the average of each interval as one output LevelRecord (one message line).
One generic object serves both messages -- they differ only in the coordinate
binned on and the interval width:

    altitude-level message (ALM):  Binner(width=5.0, key=lambda r: r.alt_asl)
    time-interval  message (TIM):  Binner(width=1.0, key=lambda r: r.time)

Everything else is shared, so it lives here once: the bin edges are aligned up
to a whole multiple of the width (the SoW rounds the first altitude up to a 5 m
multiple; time bins align the same way, to whole seconds), a bin closes when a
record's coordinate reaches the bin's upper edge, and the closed bin is averaged
and emitted while the edges advance to the record's bin.  A partially-filled top
bin -- the one still accumulating when the ascent ends -- is discarded by
reset(), never emitted.

Averaging is a plain arithmetic mean per field, except the pure attitude angles
(CIRCULAR_FIELDS: roll, pitch, yaw) use a circular mean so the +/-pi wrap can't
corrupt them.  Wind needs no special case here: derive() already carries it as
east/north vector components, so averaging those linearly *is* the correct
vector average of the wind.

Note on fidelity: the SoW (section 1.1.5) interpolates each bin's time window
from the altitude crossings and averages within it.  Because the gate only ever
feeds ascending samples, altitude rises monotonically, so binning a sample by
its own coordinate selects exactly the same samples that interpolation would --
the two are equivalent here.  If non-monotonic altitude ever had to be handled,
the altitude binner would split off and gain the interpolation.

One instance per message per connection; reset() at each ascent start.
"""

import math
from dataclasses import fields

from gcs.met_derive import LevelRecord, CIRCULAR_FIELDS

# All LevelRecord field names, in declaration order; drives the averaging.
_FIELDS = [f.name for f in fields(LevelRecord)]


def _round_up(value, step):
    """Smallest multiple of step that is >= value."""
    return math.ceil(value / step) * step


class Binner:
    """Bins a LevelRecord stream into fixed-width intervals and emits the
    per-bin average."""

    def __init__(self, width, key):
        self._width = width      # bin width (5.0 m for ALM, 1.0 s for TIM)
        self._key = key          # LevelRecord -> the coordinate to bin on
        self.reset()

    def reset(self):
        """Drop all state for a fresh ascent.

        Called at each ascent start (each ascent is its own profile / file).
        Any partially-filled top bin is discarded here -- it is never emitted.
        """
        self._low = None         # current bin's lower edge (None until first record)
        self._high = None        # current bin's upper edge
        self._bin = []           # records accumulated in the current bin

    def feed(self, record):
        """Feed one per-sample LevelRecord.

        Returns the averaged LevelRecord for a bin that just closed, else None.
        At most one bin can close per record (any bins the data skipped over are
        empty and emit nothing).
        """
        coord = self._key(record)
        if self._low is None:
            # First record establishes the grid, aligned up to a whole step.
            # The remainder below this edge (the partial first bin) is dropped.
            self._low = _round_up(coord, self._width)
            self._high = self._low + self._width

        emitted = None
        if coord >= self._high:
            # The current bin is complete: average and emit it if it has any
            # samples, then advance the edges to the bin holding this record.
            if self._bin:
                emitted = self._average(self._bin)
                self._bin = []
            while coord >= self._high:
                self._low = self._high
                self._high = self._low + self._width

        if coord >= self._low:
            self._bin.append(record)
        # coord < self._low: below the first aligned edge -> discard.
        return emitted

    def _average(self, records):
        """Average a bin's records into one LevelRecord (circular for angles)."""
        n = len(records)
        out = LevelRecord()
        for name in _FIELDS:
            if name in CIRCULAR_FIELDS:
                s = sum(math.sin(getattr(r, name)) for r in records)
                c = sum(math.cos(getattr(r, name)) for r in records)
                setattr(out, name, math.atan2(s, c))
            else:
                setattr(out, name, sum(getattr(r, name) for r in records) / n)
        return out
