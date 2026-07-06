"""
Telemetry-log replay client for CopterSonde GCS.

Reads a recorded ``.tlog`` file (8-byte big-endian microsecond timestamp
followed by a raw MAVLink frame per record — the format written by Mission
Planner, QGroundControl, MAVProxy, and pymavlink) and drives the shared
``VehicleState`` exactly as a live connection would: every parsed message
flows through the inherited ``MAVLinkClient._handle_message`` dispatch, so
state population — including wind re-derived from pitch with the current
``ws_a``/``ws_b`` coefficients — is byte-for-byte the live code path.

The subclass replaces the transport, not the brains: it opens the log file
instead of a UDP socket, paces playback at 1x real time from the recorded
timestamps, and transmits nothing, ever.  All command methods are safe
no-ops during replay.
"""

import os
import threading
import time

from pymavlink import mavutil

from gcs.logutil import get_logger
from gcs.mavlink_client import (
    DATA_EMIT_INTERVAL_S,
    SEVERITY_NAMES,
    MAVLinkClient,
)
from gcs.vehicle_state import StatusMessage

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Cap on the sleep between consecutive messages — long recording gaps
# (e.g. the vehicle sat idle between profiles) replay as a short pause
# instead of stalling playback for minutes.
REPLAY_MAX_GAP_S = 2.0
# Inter-message sleeps are sliced so stop() stays responsive (~100 ms).
REPLAY_SLEEP_SLICE_S = 0.05
# MAV_SEVERITY_NOTICE — severity of the synthetic "Replay complete" message.
_NOTICE_SEVERITY = 5

log = get_logger("tlog_replay")


class TlogReplayClient(MAVLinkClient):
    """
    Replay client that feeds a recorded ``.tlog`` through the live
    message-handling path.

    Lifecycle: ``start(filepath, generate_logs)`` opens the file and spawns
    a daemon replay thread; at end of file ``finished`` becomes True and the
    final state stays frozen (no auto-reset, no looping) while ``running``
    remains True until ``stop()`` is called.
    """

    def __init__(self, state=None, event_bus=None):
        super().__init__(state=state, event_bus=event_bus)
        # Replay-specific fields (everything else comes from the parent)
        self.finished = False        # True once playback reached end of file
        self._filepath = None        # absolute path of the loaded .tlog
        self._last_data_emit = 0.0   # monotonic time of last DATA_UPDATED

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, filepath: str, generate_logs: bool,
              emit_raw: bool = True, emit_alm: bool = True,
              emit_tim: bool = True, emit_wmo: bool = True):
        """Open ``filepath`` and start the paced replay thread.

        Raises on failure to open the file (missing/unreadable path) — the
        caller wraps this, as it already does for the live client.  When
        ``generate_logs`` is False the per-session MAVLink message log is
        never opened, so no log file is created for the replay.

        ``emit_raw`` / ``emit_alm`` / ``emit_tim`` / ``emit_wmo`` are the
        user's per-message replay-output toggles: each gates whether that
        message file is produced for this replay.  They default to True so a
        bare ``start(path, generate_logs)`` call reproduces the prior
        always-on behavior.  These only ever affect replay — a live
        connection always produces every message.
        """
        if self.running:
            log.warning("start() called but replay already running")
            return

        # Apply the per-message output enables before the replay thread starts,
        # since _handle_message (on the replay thread) reads them as data flows.
        self._emit_raw = emit_raw
        self._emit_alm = emit_alm
        self._emit_tim = emit_tim
        self._emit_wmo = emit_wmo

        log.info("Opening tlog for replay: %s", filepath)
        # Open first so failures propagate before any state is touched.
        # For an existing file pymavlink returns a mavmmaplog: it pre-scans
        # the file at open, then recv_match(blocking=False) yields parsed
        # messages one at a time (None at end of file) with msg._timestamp
        # set from the recorded 8-byte value.
        self._conn = mavutil.mavlink_connection(filepath)
        self._filepath = filepath

        # Fresh state for the new session — same guarantee as a live connect.
        # The replay client is one long-lived instance reused for every file, so
        # the balancer and ascent gate are reset here too; otherwise a second
        # replay would keep the first file's time-sync offset (corrupting every
        # output timestamp) and its leftover ascent state.  MAVLinkClient.start()
        # resets the balancer and gate the same way for a live connection.
        self.state.reset()
        self._balancer.reset()
        self._gate.reset()

        # Reset diagnostics (mirrors the parent's start()).
        self.msg_count = 0
        self._first_msg_time = None
        self._connect_time = time.monotonic()
        self._last_watchdog_log = 0
        self.last_heartbeat_time = 0.0
        self._streams_requested = False
        self.finished = False

        # Per-session message log is opt-in for replay; log_message() and
        # close() are safe no-ops when open() was never called.
        if generate_logs:
            self._msg_logger.open()

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._replay_loop, name="tlog-replay", daemon=True
        )
        self._thread.start()
        self.running = True
        log.info("Tlog replay thread started: %s", self.filename)

        if self.event_bus:
            from gcs.event_bus import EventType
            self.event_bus.emit(EventType.CONNECTION_CHANGED,
                                {"connected": True, "replay": True})

    def stop(self):
        """Stop playback.  Idempotent and safe to call at any time."""
        if not self.running:
            return
        log.info("Stopping tlog replay …")
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        if self._conn is not None:
            # mavmmaplog.close() closes the file handle and the mmap; guard
            # defensively in case a future pymavlink changes the semantics.
            try:
                self._conn.close()
            except Exception:
                log.exception("Error closing tlog connection")
            self._conn = None
        self.running = False
        self._streams_requested = False

        # Finalize the per-session log and the message writers (each close() is a
        # no-op if never opened, or already closed at end of file).
        self._msg_logger.close()
        self._finalize_message_writers()

        log.info("Tlog replay stopped")

        if self.event_bus:
            from gcs.event_bus import EventType
            self.event_bus.emit(EventType.CONNECTION_CHANGED,
                                {"connected": False})

    @property
    def progress(self):
        """Playback position as a float 0-100 (100 once finished)."""
        if self.finished:
            return 100.0
        conn = self._conn
        if conn is None:
            return 0.0
        try:
            pct = float(getattr(conn, "percent", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(100.0, pct))

    @property
    def filename(self):
        """Basename of the loaded file, '' before any load."""
        return os.path.basename(self._filepath) if self._filepath else ""

    # ------------------------------------------------------------------
    # Background replay loop
    # ------------------------------------------------------------------

    def _replay_loop(self):
        """Paced replay loop — runs in the 'tlog-replay' daemon thread.

        Reads messages non-blocking (file connections return the next
        message immediately and None at end of file), sleeps the recorded
        inter-message gap capped at REPLAY_MAX_GAP_S, and dispatches every
        message — BAD_DATA included — through the inherited live handler.
        """
        conn = self._conn
        prev_ts = None
        self._last_data_emit = 0.0

        while not self._stop_event.is_set():
            try:
                msg = conn.recv_match(blocking=False)
            except Exception:
                log.exception("Replay read error — ending playback")
                msg = None
            if msg is None:
                break  # end of file

            # --- Pace at 1x from consecutive recorded timestamps ---
            ts = getattr(msg, "_timestamp", None)
            if ts is not None and prev_ts is not None:
                gap = min(max(ts - prev_ts, 0.0), REPLAY_MAX_GAP_S)
                if gap > 0.0 and not self._sleep_interruptible(gap):
                    return  # stopped mid-gap; stop() owns teardown
            # BAD_DATA stamps can be mis-framed garbage, so only clean
            # messages advance the pacing reference (pymavlink applies the
            # same policy to its own internal _last_timestamp).
            if ts is not None and msg.get_type() != "BAD_DATA":
                prev_ts = ts

            # --- Dispatch through the live code path ---
            self._handle_message(msg)

            # --- Emit data event at ~10 Hz (only if someone is listening) ---
            self._maybe_emit_data()

        if self._stop_event.is_set():
            return  # interrupted by stop() — not an end-of-file
        self._finish_replay()

    def _sleep_interruptible(self, duration):
        """Sleep ``duration`` seconds in short slices, watching the stop
        event so stop() responds within ~100 ms.  Returns False if stopped.

        The ~10 Hz data emission keeps ticking through the slices — like
        the live loop, the UI refresh clock is independent of message
        arrival cadence, so sparse recordings still render smoothly.
        """
        deadline = time.monotonic() + duration
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return True
            if self._stop_event.wait(min(remaining, REPLAY_SLEEP_SLICE_S)):
                return False
            self._maybe_emit_data()

    def _maybe_emit_data(self):
        """Emit DATA_UPDATED if the ~10 Hz interval has elapsed, guarded by
        has_subscribers() to skip snapshot overhead when nobody listens."""
        if not self.event_bus:
            return
        now = time.monotonic()
        if now - self._last_data_emit < DATA_EMIT_INTERVAL_S:
            return
        from gcs.event_bus import EventType
        if self.event_bus.has_subscribers(EventType.DATA_UPDATED):
            self.event_bus.emit(EventType.DATA_UPDATED,
                                self.state.snapshot())
        self._last_data_emit = now

    def _finish_replay(self):
        """End-of-file: mark finished and freeze the final state.

        ``running`` stays True so the UI keeps showing the last frame until
        the user explicitly stops the replay.
        """
        self.finished = True

        sm = StatusMessage(
            severity=_NOTICE_SEVERITY,
            severity_name=SEVERITY_NAMES.get(_NOTICE_SEVERITY, "NOTICE"),
            text="Replay complete",
            timestamp=time.time(),
        )
        self.state.status_messages.append(sm)
        if len(self.state.status_messages) > 200:
            self.state.status_messages = self.state.status_messages[-200:]
        log.info("Replay complete: %s (%d messages)",
                 self.filename, self.msg_count)

        # The last message has been written — finalize the outputs now so they
        # are complete even before stop() is called: the per-session log carries
        # its EOF marker and the message writers are flushed.
        self._msg_logger.close()
        self._finalize_message_writers()

        # One final data event so subscribers render the frozen end state.
        if self.event_bus:
            from gcs.event_bus import EventType
            if self.event_bus.has_subscribers(EventType.DATA_UPDATED):
                self.event_bus.emit(EventType.DATA_UPDATED,
                                    self.state.snapshot())

    def _finalize_message_writers(self):
        """Flush and close the replay's RAW / ALM / TIM / WMO output writers.

        Mirrors the live client's stop() finalization.  It matters most for the
        WMO netCDF, which is written only at close(): an ascent still open at end
        of file -- the common single-profile case, where the recording stops
        before a descent is detected -- would otherwise never produce its .nc,
        and the RAW/ALM/TIM file handles would leak.  The .tlog writer is omitted
        on purpose: replay never opens it (see _open_telemetry_log).  Every
        close() is an idempotent, thread-safe no-op when its writer is already
        closed, so calling this from both _finish_replay() and stop() is safe.
        """
        self._raw_writer.close()
        self._alm_writer.close()
        self._tim_writer.close()
        self._wmo_writer.close()

    # ------------------------------------------------------------------
    # Transmission overrides — replay sends nothing, ever
    # ------------------------------------------------------------------

    def _send_gcs_heartbeat(self):
        """No-op: replay transmits nothing."""

    def _request_data_streams(self):
        """No-op: replay transmits nothing (called on first heartbeat)."""

    def send_command_long(self, command, p1=0, p2=0, p3=0, p4=0, p5=0, p6=0, p7=0):
        """No-op: commands are not available during replay."""

    def arm(self):
        """No-op: commands are not available during replay."""

    def disarm(self):
        """No-op: commands are not available during replay."""

    def set_mode(self, mode_name: str):
        """No-op: commands are not available during replay."""

    def takeoff(self, alt_m: float = 10.0):
        """No-op: commands are not available during replay."""

    def set_param(self, name: str, value: float, param_type=None):
        """No-op: commands are not available during replay."""

    def request_all_params(self):
        """No-op: commands are not available during replay."""

    def set_rc_override(self, channel: int, pwm_value: int):
        """No-op: commands are not available during replay."""

    def trigger_autovp(self, target_altitude: float, on_done=None):
        """No-op: reports unavailability through the standard callback."""
        if on_done:
            on_done(False, "Not available during replay")

    def arm_and_takeoff_auto(self, on_done=None):
        """No-op: reports unavailability through the standard callback."""
        if on_done:
            on_done(False, "Not available during replay")

    def fetch_log(self, on_progress=None, on_done=None):
        """No-op: reports unavailability through the standard callback."""
        if on_done:
            on_done(False, "Not available during replay")

    def _open_telemetry_log(self, start_time):
        """No fresh .tlog during replay; open the RAW file only if enabled.

        The live client opens its telemetry log AND its RAW message file on
        first-data arrival via this hook.  Replay must never spawn a fresh
        .tlog (that would re-record the recording), so the .tlog writer is
        deliberately left closed — every inherited ``_tlog_writer.log_message``
        call then stays a no-op.  The RAW file, by contrast, is a replay
        output the user can ask for: open it when ``_emit_raw`` is set, so the
        inherited ``_raw_writer.write_row`` actually writes.  ``serial`` and
        ``start_time`` are forwarded like the live path, so the replay's RAW
        file carries the drone serial and is named from the recording's time,
        not wall-clock.  (Opt-in human-readable logging is handled separately
        in ``start``.)
        """
        if self._emit_raw:
            self._raw_writer.open(serial=self.drone_serial, start_time=start_time)