"""
Read-only web dashboard for CopterSonde GCS — proof of concept.

Serves a single HTML page over HTTP and pushes live state to it over a
WebSocket, so a laptop joined to the Herelink's WiFi hotspot can watch
telemetry in a browser with nothing installed:

    http://192.168.43.1:8000/        (Herelink hotspot address)
    http://<host-ip>:8000/           (desktop testing)

Design constraints for this PoC:
  - stdlib only: no new dependencies, so the Android/python-for-android
    build is untouched.
  - Read-only by construction: the server exposes exactly two GET
    endpoints (the page and the socket) and never parses client
    payloads into actions.  There is nothing a client can send that
    changes GCS or vehicle state.
  - Always on: started at app launch, independent of vehicle
    connection state.  When no source is running the page still
    updates (GCS clock / heartbeat counter), which proves liveness.

WebSocket support is a minimal server-side implementation of RFC 6455:
handshake + unmasked server->client text frames + close handling.
Browser clients don't send pings and this GCS never expects inbound
data frames, so client frames other than CLOSE are read and discarded.

Threading: ThreadingHTTPServer gives each connection its own daemon
thread; the per-client push loop runs there.  The state provider is
called from those threads, so it must only read (see main.py's
_dashboard_state) — simple attribute reads on VehicleState are safe.
"""

import base64
import hashlib
import json
import select
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gcs.logutil import get_logger

log = get_logger("web_dash")

DEFAULT_PORT = 8000
PUSH_INTERVAL_S = 0.5          # state push rate to each client (2 Hz)
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # fixed by RFC 6455

# ---------------------------------------------------------------------------
# The page.  Inlined so there is nothing to package or resolve on Android.
# ---------------------------------------------------------------------------
_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CopterSonde Live</title>
<style>
  body { background:#14171c; color:#e8e8e8; font-family:system-ui,sans-serif;
         display:flex; flex-direction:column; align-items:center;
         justify-content:center; min-height:95vh; margin:0; }
  #status { padding:4px 14px; border-radius:12px; font-size:14px;
            background:#5a2626; margin-bottom:24px; }
  #status.up { background:#265a2e; }
  #voltage { font-size:96px; font-weight:700; font-variant-numeric:tabular-nums;
             line-height:1; }
  #voltage span { font-size:36px; color:#9aa4b0; }
  .row { margin-top:18px; font-size:16px; color:#9aa4b0;
         font-variant-numeric:tabular-nums; }
  .row b { color:#e8e8e8; font-weight:600; }
</style>
</head>
<body>
  <div id="status">connecting&hellip;</div>
  <div id="voltage">&ndash;.&ndash;&ndash;<span> V</span></div>
  <div class="row">Vehicle link: <b id="vlink">&ndash;</b></div>
  <div class="row">GCS time: <b id="gtime">&ndash;</b></div>
  <div class="row">Messages received: <b id="msgs">&ndash;</b></div>
<script>
  const el = id => document.getElementById(id);
  function connect() {
    const ws = new WebSocket("ws://" + location.host + "/ws");
    ws.onopen = () => {
      el("status").textContent = "live";
      el("status").className = "up";
    };
    ws.onmessage = ev => {
      const d = JSON.parse(ev.data);
      el("voltage").innerHTML =
          d.voltage.toFixed(2) + "<span> V</span>";
      el("vlink").textContent = d.connected ? "connected" : "not connected";
      el("gtime").textContent = d.gcs_time;
      el("msgs").textContent = d.msg_count;
    };
    ws.onclose = () => {
      el("status").textContent = "disconnected \\u2014 retrying\\u2026";
      el("status").className = "";
      setTimeout(connect, 1000);   // auto-reconnect
    };
  }
  connect();
</script>
</body>
</html>
"""


def _ws_text_frame(text):
    """Encode one unmasked server->client TEXT frame (RFC 6455 s5.2)."""
    payload = text.encode("utf-8")
    n = len(payload)
    if n < 126:
        header = struct.pack("!BB", 0x81, n)
    elif n < 65536:
        header = struct.pack("!BBH", 0x81, 126, n)
    else:
        header = struct.pack("!BBQ", 0x81, 127, n)
    return header + payload


class _Handler(BaseHTTPRequestHandler):
    server_version = "CoperSondeDash/0.1"

    # Route http.server's stderr chatter into the app log instead.
    def log_message(self, fmt, *args):
        log.debug("HTTP %s %s", self.client_address[0], fmt % args)

    def do_GET(self):
        if self.path == "/ws":
            self._serve_websocket()
        elif self.path in ("/", "/index.html"):
            body = _PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    # ── WebSocket ──

    def _serve_websocket(self):
        key = self.headers.get("Sec-WebSocket-Key")
        upgrade = self.headers.get("Upgrade", "")
        if not key or "websocket" not in upgrade.lower():
            self.send_error(400, "WebSocket upgrade required")
            return

        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        # Hand-rolled 101 response: send_response() would append headers
        # (Date, Server) after end_headers() is called, which is fine, but
        # writing the raw bytes keeps the handshake exact and unbuffered.
        self.connection.sendall(
            ("HTTP/1.1 101 Switching Protocols\r\n"
             "Upgrade: websocket\r\n"
             "Connection: Upgrade\r\n"
             f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode("ascii"))
        self.close_connection = True  # we own the socket from here on

        dash = self.server.dashboard
        log.info("Dashboard client connected: %s", self.client_address[0])
        try:
            dash.push_loop(self.connection)
        except Exception:
            # A vanished client is normal (laptop lid closed, browser tab
            # killed); anything else is still not worth more than a log line.
            log.debug("Dashboard client loop ended with error", exc_info=True)
        finally:
            log.info("Dashboard client disconnected: %s",
                     self.client_address[0])


class WebDashboard:
    """Embedded HTTP + WebSocket server pushing read-only state.

    ``state_provider`` is a zero-argument callable returning a
    JSON-serialisable dict; it is invoked from client threads and must
    only read shared state, never mutate it.
    """

    def __init__(self, state_provider, port=DEFAULT_PORT,
                 push_interval_s=PUSH_INTERVAL_S):
        self._state_provider = state_provider
        self._port = port
        self._push_interval = push_interval_s
        self._httpd = None
        self._thread = None
        self.running = False

    def start(self):
        if self.running:
            return
        try:
            self._httpd = ThreadingHTTPServer(("0.0.0.0", self._port),
                                              _Handler)
        except OSError:
            # Port taken or bind refused — the dashboard is a convenience,
            # never worth blocking app startup over.
            log.exception("Web dashboard failed to bind port %d; "
                          "dashboard disabled", self._port)
            self._httpd = None
            return
        self._httpd.daemon_threads = True   # don't block process exit
        self._httpd.dashboard = self        # handler back-reference
        self.running = True
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="web-dashboard", daemon=True)
        self._thread.start()
        log.info("Web dashboard listening on http://0.0.0.0:%d "
                 "(hotspot clients: http://192.168.43.1:%d)",
                 self._port, self._port)

    def stop(self):
        if not self.running:
            return
        self.running = False           # ends all per-client push loops
        try:
            self._httpd.shutdown()     # returns after serve_forever exits
            self._httpd.server_close()
        except Exception:
            log.exception("Web dashboard shutdown error")
        self._httpd = None
        self._thread = None
        log.info("Web dashboard stopped")

    # ── Per-client push loop (runs on that client's handler thread) ──

    def push_loop(self, conn):
        last_push = 0.0
        while self.running:
            # Watch for inbound bytes so a browser CLOSE (or a dead
            # socket) is noticed promptly; the timeout paces the loop.
            readable, _, _ = select.select([conn], [], [], 0.1)
            if readable:
                data = conn.recv(4096)
                if not data:
                    return                       # peer closed the TCP stream
                if (data[0] & 0x0F) == 0x08:     # CLOSE frame
                    try:
                        conn.sendall(b"\x88\x00")  # echo close, empty payload
                    except OSError:
                        pass
                    return
                # Anything else (masked pongs/text) is ignored: this
                # endpoint is read-only and expects no client data.

            now = time.monotonic()
            if now - last_push >= self._push_interval:
                last_push = now
                try:
                    state = self._state_provider()
                except Exception:
                    log.exception("Dashboard state provider failed")
                    state = {"error": "state unavailable"}
                conn.sendall(_ws_text_frame(json.dumps(state)))
