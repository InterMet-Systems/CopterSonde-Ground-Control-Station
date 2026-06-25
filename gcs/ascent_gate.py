"""
Ascent detection and gating (SoW section 1.1.4).

The aircraft is ASCENDING when it is flying the automated profile AND climbing:

    custom_mode == 3 (AUTO)   and   vz < -2.5 m/s

vz is NED down-positive, so a value below -2.5 means climbing faster than
2.5 m/s.  Both fields ride on the BalancedLine -- custom_mode carried from the
HEARTBEAT, vz the averaged LOCAL_POSITION_NED down-velocity -- so detection runs
on the clean balanced stream, not on raw messages.

This gate is the fork point of the met pipeline.  Only ascending lines proceed
to the profile outputs, and the per-connection raw file writes a row for each
ascending line.  The gate also marks the edges of each ascent (its first and
last ascending line) so the downstream per-ascent files can be opened and
closed -- and the partial top bin discarded -- at the right moments.  Those edge
actions are left to the caller through on_start / on_end, so the gate itself
stays pure detection and is trivially testable.

Interface (one instance per connection; reset() on connect):
    out = gate.feed(line)         # the line while ascending, else None
    AscentGate(on_start, on_end)  # each fires once, with the 1-based ascent number
"""

AUTO_MODE = 3        # ArduCopter AUTO custom_mode -- the automated profile flies here
ASCENT_VZ = -2.5     # m/s; vz is NED down-positive, so < this means climbing


class AscentGate:
    """Detects ascent on the balanced stream and forks it (SoW 1.1.4).

    A plain injected instance, one per connection (reset() on connect), holding
    only the current ascending flag and the ascent counter -- the same lifecycle
    as the balancer and the writers.
    """

    def __init__(self, on_start=None, on_end=None):
        # on_start(n) / on_end(n) fire once at the first / last ascending line of
        # ascent n (1-based).  They are the hooks for opening and closing this
        # ascent's profile files; leaving them unset makes the gate detection-only.
        self._on_start = on_start
        self._on_end = on_end
        self.reset()

    def reset(self):
        """Clear ascent state for a new connection."""
        self._ascending = False
        self._count = 0

    @property
    def ascending(self):
        """True while the aircraft is in an ascent."""
        return self._ascending

    @property
    def ascent_number(self):
        """How many ascents have started this connection (1-based once climbing)."""
        return self._count

    def feed(self, line):
        """Feed one BalancedLine.

        Returns the line while ascending (for the raw row and the profile
        outputs), else None.  Fires on_start(n) / on_end(n) once at each ascent's
        leading and trailing edge.
        """
        ascending = (line.custom_mode == AUTO_MODE) and (line.vz < ASCENT_VZ)
        if ascending and not self._ascending:
            self._ascending = True
            self._count += 1
            if self._on_start is not None:
                self._on_start(self._count)
        elif not ascending and self._ascending:
            self._ascending = False
            if self._on_end is not None:
                self._on_end(self._count)
        return line if ascending else None
