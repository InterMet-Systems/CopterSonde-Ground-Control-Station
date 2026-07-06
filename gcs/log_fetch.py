"""
Drone-log download over the MAVLink log-transfer protocol (SoW 205195 §1.4).

The drone records LOG.BIN files on its own microSD; this fetches the most
recent one (#12) and saves it under [usr access intended]/DroneLog (#13),
named ``{id:08d}.BIN`` from the LOG_ENTRY id since the protocol carries no
filenames (#14).

Protocol: LOG_REQUEST_LIST -> LOG_ENTRY (id, size, last_log_num), then
LOG_REQUEST_DATA -> a stream of LOG_DATA chunks (90 bytes each, tagged with
their byte offset), ended by LOG_REQUEST_END.  A 1 MB log is ~11,600 LOG_DATA
messages over lossy UDP, so chunks are tracked individually in a bitmap and
any hole left by packet loss is re-requested after a stall — the file is
written only when every byte is accounted for, never from a single
optimistic pass.

Threading: every method except start() is called from the client's IO
thread (message handlers + the tick in the IO loop).  start() only flags
the request; the first transmission happens from tick(), so all protocol
sends stay on the IO thread.  Callbacks fire on the IO thread — UI code
must reschedule onto the main thread (the screens already do this for the
command callbacks).
"""

import os
import time

from gcs.logutil import get_logger
from gcs.storage_paths import mirror_file, output_dirs

log = get_logger("log_fetch")

CHUNK = 90                    # LOG_DATA payload size (fixed by the message)
LIST_TIMEOUT_S = 2.0          # resend LOG_REQUEST_LIST if no entry arrives
LIST_RETRIES = 3              # list attempts before giving up
STALL_S = 1.5                 # no LOG_DATA for this long -> re-request holes
MAX_STALLS_NO_PROGRESS = 10   # consecutive zero-progress stalls -> fail
OVERALL_TIMEOUT_S = 300.0     # hard cap on a whole download
PROGRESS_INTERVAL_S = 0.3     # min seconds between progress callbacks


class LogFetcher:
    """One in-flight download of the drone's most recent log.

    Owned by MAVLinkClient; fed by its LOG_ENTRY / LOG_DATA handlers and
    ticked from its IO loop.  Idle when no download is active — tick() is
    then a single state check.
    """

    def __init__(self, client):
        self._client = client
        self._state = "idle"      # idle | pending | listing | downloading
        self._on_progress = None
        self._on_done = None

    @property
    def active(self):
        return self._state != "idle"

    # ── UI entry point (main thread) ──────────────────────────────────

    def start(self, on_progress=None, on_done=None):
        """Request a download; refused if one is already running."""
        if self._state != "idle":
            if on_done:
                on_done(False, "A log download is already in progress")
            return
        self._on_progress = on_progress
        self._on_done = on_done
        self._t_start = time.monotonic()
        self._t_last = 0.0            # last list request / last LOG_DATA
        self._list_tries = 0
        self._entries = {}            # log id -> size
        self._last_log_num = None
        self._log_id = None
        self._size = 0
        self._buf = None
        self._chunks = None           # bytearray bitmap, 1 = chunk received
        self._missing = 0
        self._stalls = 0
        self._progressed = False      # any new chunk since the last stall?
        self._t_progress = 0.0
        self._state = "pending"       # tick() sends the first request

    # ── Message handlers (IO thread) ──────────────────────────────────

    def on_log_entry(self, msg):
        if self._state != "listing":
            return
        if msg.num_logs == 0:
            self._fail("No logs on drone")
            return
        self._entries[msg.id] = msg.size
        self._last_log_num = msg.last_log_num
        if self._last_log_num in self._entries:
            self._begin_download()

    def on_log_data(self, msg):
        if self._state != "downloading" or msg.id != self._log_id:
            return
        self._t_last = time.monotonic()
        ofs, count = msg.ofs, msg.count
        if ofs >= self._size or count <= 0:
            return
        count = min(count, self._size - ofs)
        self._buf[ofs:ofs + count] = bytes(msg.data[:count])
        # Mark every chunk whose full extent lies inside [ofs, ofs+count).
        # In the normal aligned stream that is simply every chunk delivered,
        # including the final partial one; misaligned segments can only
        # under-mark, never falsely complete a chunk.
        i = ofs // CHUNK if ofs % CHUNK == 0 else ofs // CHUNK + 1
        end = ofs + count
        while i * CHUNK < end and (min((i + 1) * CHUNK, self._size) <= end):
            if not self._chunks[i]:
                self._chunks[i] = 1
                self._missing -= 1
                self._progressed = True
            i += 1
        if self._missing <= 0:
            self._finish()
        else:
            self._report_progress()

    # ── Periodic driver (IO thread, from the client's IO loop) ────────

    def tick(self, now):
        if self._state == "idle":
            return
        if now - self._t_start > OVERALL_TIMEOUT_S:
            self._fail("Timed out")
            return

        if self._state == "pending":
            self._state = "listing"
            self._send_list_request()
            self._t_last = now

        elif self._state == "listing":
            if now - self._t_last > LIST_TIMEOUT_S:
                self._list_tries += 1
                if self._list_tries >= LIST_RETRIES:
                    self._fail("No response to log list request")
                    return
                self._send_list_request()
                self._t_last = now

        elif self._state == "downloading":
            if now - self._t_last > STALL_S:
                if self._progressed:
                    self._stalls = 0
                else:
                    self._stalls += 1
                    if self._stalls >= MAX_STALLS_NO_PROGRESS:
                        self._fail("Link lost during download")
                        return
                self._progressed = False
                self._request_from_first_hole()
                self._t_last = now

    # ── Abort (any thread, via client.stop()) ─────────────────────────

    def abort(self, reason):
        if self._state != "idle":
            self._fail(reason, send_end=False)

    # ── Internals ─────────────────────────────────────────────────────

    def _begin_download(self):
        self._log_id = self._last_log_num
        self._size = self._entries[self._log_id]
        log.info("Fetching drone log %08d.BIN (%d bytes)",
                 self._log_id, self._size)
        if self._size == 0:
            # Legal but odd: an empty log. Write it as-is.
            self._buf = bytearray()
            self._finish()
            return
        self._buf = bytearray(self._size)
        n_chunks = (self._size + CHUNK - 1) // CHUNK
        self._chunks = bytearray(n_chunks)
        self._missing = n_chunks
        self._state = "downloading"
        self._t_last = time.monotonic()
        self._send_data_request(0)

    def _request_from_first_hole(self):
        hole = self._chunks.find(0)
        if hole < 0:               # nothing missing; completion is imminent
            return
        self._send_data_request(hole * CHUNK)

    def _report_progress(self):
        if not self._on_progress:
            return
        now = time.monotonic()
        if now - self._t_progress < PROGRESS_INTERVAL_S:
            return
        self._t_progress = now
        done = len(self._chunks) - self._missing
        pct = 100.0 * done / len(self._chunks)
        self._on_progress(pct, self._size)

    def _finish(self):
        try:
            pdir, bdir = output_dirs("DroneLog")
            os.makedirs(pdir, exist_ok=True)
            name = f"{self._log_id:08d}.BIN"
            path = os.path.join(pdir, name)
            # Preserve the drone's filename (#14); suffix only on collision
            # (e.g. a re-download, or a different drone reusing the id).
            n = 1
            while os.path.exists(path):
                path = os.path.join(pdir, f"{self._log_id:08d}_{n}.BIN")
                n += 1
            with open(path, "wb") as fh:
                fh.write(self._buf)
            mirror_file(path, bdir)
        except Exception as exc:
            log.exception("Failed to save drone log")
            self._fail(f"Save failed: {exc}")
            return
        log.info("Drone log saved: %s (%d bytes)", path, len(self._buf))
        self._send_end_request()
        on_done = self._on_done
        self._reset()
        if on_done:
            on_done(True, path)

    def _fail(self, reason, send_end=True):
        log.warning("Drone log fetch failed: %s", reason)
        if send_end and self._state == "downloading":
            self._send_end_request()
        on_done = self._on_done
        self._reset()
        if on_done:
            on_done(False, reason)

    def _reset(self):
        self._state = "idle"
        self._on_progress = None
        self._on_done = None
        self._buf = None
        self._chunks = None

    # ── Transmission (best-effort; a dead link surfaces as a stall) ───

    def _target(self):
        c = self._client
        return (c.last_sysid or 1, c.last_compid or 1)

    def _send_list_request(self):
        conn = self._client._conn
        if conn is None:
            self._fail("Not connected", send_end=False)
            return
        try:
            sysid, compid = self._target()
            if self._last_log_num is not None:
                # Retry after partial loss: ask for just the target entry.
                conn.mav.log_request_list_send(
                    sysid, compid, self._last_log_num, self._last_log_num)
            else:
                conn.mav.log_request_list_send(sysid, compid, 0, 0xFFFF)
        except Exception:
            log.exception("LOG_REQUEST_LIST send failed")

    def _send_data_request(self, ofs):
        conn = self._client._conn
        if conn is None:
            self._fail("Not connected", send_end=False)
            return
        try:
            sysid, compid = self._target()
            conn.mav.log_request_data_send(
                sysid, compid, self._log_id, ofs, 0xFFFFFFFF)
        except Exception:
            log.exception("LOG_REQUEST_DATA send failed")

    def _send_end_request(self):
        conn = self._client._conn
        if conn is None:
            return
        try:
            sysid, compid = self._target()
            conn.mav.log_request_end_send(sysid, compid)
        except Exception:
            log.exception("LOG_REQUEST_END send failed")
