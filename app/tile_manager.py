"""
Slippy-map tile manager for CopterSonde GCS.

Downloads, caches, and serves satellite + hybrid overlay tiles
from ArcGIS World Imagery.  Uses Spherical Mercator (EPSG:3857).
"""

import math
import os
import ssl
import threading
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from kivy.clock import Clock

from gcs.logutil import get_logger

log = get_logger("tile_manager")


# ── SSL context setup ─────────────────────────────────────────────
# Android's Python build often lacks a usable system CA bundle,
# so we try certifi first, then fall back to unverified SSL.
# This is acceptable because tile CDNs are read-only public data.


def _make_tile_ssl_context():
    """Create an SSL context for tile CDN downloads.

    On Android, the default CA bundle is often missing.  Try certifi
    first, then fall back to an unverified context — acceptable here
    because tile servers are read-only public CDNs.
    """
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        log.info("SSL: using certifi CA bundle for tile downloads")
        return ctx
    except Exception:
        pass
    # Default context uses system CA store — works on desktop but may
    # fail on Android where the system store isn't accessible to Python.
    # We return a permissive context so tile downloads don't break.
    log.warning("certifi not available — using unverified SSL for tiles")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# Module-level singleton — created once at import time
_tile_ssl_ctx = _make_tile_ssl_context()

TILE_SIZE = 256       # Standard Web Mercator tile dimension in pixels
MIN_ZOOM = 1
MAX_ZOOM = 19         # ArcGIS imagery resolution limit
DEFAULT_ZOOM = 15

# ArcGIS tile servers (free, no API key required)
SATELLITE_URL = (
    "https://services.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
OVERLAY_URL = (
    "https://services.arcgisonline.com/ArcGIS/rest/services/"
    "Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}"
)

# Spherical Mercator projects the globe onto a square; latitudes beyond
# ~85.05 degrees cause the projection to diverge to infinity.
_MAX_LAT = 85.05112878  # Mercator latitude limit


# ── Mercator projection helpers ─────────────────────────────────────
# All three functions implement the standard Web Mercator (EPSG:3857)
# formulas used by Google Maps, OSM, and ArcGIS tile servers.
# px_x = (lon + 180) / 360 * 2^zoom * TILE_SIZE
# px_y = (1 - asinh(tan(lat)) / pi) / 2 * 2^zoom * TILE_SIZE
# The Y formula maps lat to [0, 1] via the Mercator integral, then
# scales to pixel space. Y increases downward (north = small Y).


def lat_lon_to_pixel(lat, lon, zoom):
    """Convert lat/lon to global Mercator pixel coordinates."""
    n = 2.0 ** zoom
    px = (lon + 180.0) / 360.0 * n * TILE_SIZE
    lat_c = max(-_MAX_LAT, min(_MAX_LAT, lat))  # clamp to avoid divergence
    lat_rad = math.radians(lat_c)
    # asinh(tan(lat)) is the Mercator integral of sec(lat)
    py = ((1.0 - math.asinh(math.tan(lat_rad)) / math.pi)
          / 2.0 * n * TILE_SIZE)
    return px, py


def lat_lon_to_tile(lat, lon, zoom):
    """Convert lat/lon to tile (x, y) at given zoom."""
    n = 2.0 ** zoom
    tx = int((lon + 180.0) / 360.0 * n)
    lat_c = max(-_MAX_LAT, min(_MAX_LAT, lat))
    lat_rad = math.radians(lat_c)
    ty = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    max_tile = int(n) - 1
    return max(0, min(tx, max_tile)), max(0, min(ty, max_tile))


def tile_to_lat_lon(tx, ty, zoom):
    """Return NW corner lat/lon of tile (tx, ty) at zoom."""
    n = 2.0 ** zoom
    lon = tx / n * 360.0 - 180.0
    # Inverse Mercator: convert Y fraction back to latitude
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
    return lat, lon


# ── Tile cache (memory LRU + disk) ──────────────────────────────────
# Two-level design: fast in-memory LRU for recently viewed tiles,
# plus persistent disk storage so tiles survive app restarts.
# Memory layer avoids repeated disk I/O when panning back and forth;
# disk layer avoids repeated network downloads across sessions.


class TileCache:
    """Two-level tile cache: in-memory LRU + persistent disk storage."""

    def __init__(self, cache_dir, max_memory=200):
        self._dir = cache_dir
        self._max = max_memory          # max tiles held in RAM
        self._mem = OrderedDict()       # LRU: oldest first via OrderedDict
        self._lock = threading.Lock()   # guards _mem from worker threads
        os.makedirs(cache_dir, exist_ok=True)

    def _disk_path(self, z, x, y):
        return os.path.join(self._dir, str(z), f"{x}_{y}.png")

    def get(self, z, x, y):
        """Return tile bytes or None."""
        key = (z, x, y)
        # Check memory first (fast path)
        with self._lock:
            if key in self._mem:
                self._mem.move_to_end(key)  # mark as recently used
                return self._mem[key]
        # Fall through to disk (slower path)
        path = self._disk_path(z, x, y)
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    data = f.read()
                # Promote disk hit into memory cache
                with self._lock:
                    self._mem[key] = data
                    self._evict()
                return data
            except OSError:
                pass
        return None

    def put(self, z, x, y, data):
        """Store tile bytes in memory and on disk."""
        key = (z, x, y)
        with self._lock:
            self._mem[key] = data
            self._evict()
        # Write-through to disk for persistence across restarts
        path = self._disk_path(z, x, y)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        except OSError:
            pass

    def _evict(self):
        # Drop oldest entries when memory budget is exceeded
        while len(self._mem) > self._max:
            self._mem.popitem(last=False)  # FIFO eviction = LRU


# ── Tile downloader (threaded) ──────────────────────────────────────
# Uses a bounded ThreadPoolExecutor (4 workers) to avoid flooding
# the CDN or exhausting file descriptors.  A _pending set prevents
# duplicate downloads for the same tile.  After 20 consecutive
# failures the downloader enters offline mode to stop wasting
# resources; call reset_offline() to re-enable downloads.


class TileDownloader:
    """Background tile downloader with bounded thread pool."""

    def __init__(self, sat_cache, ovl_cache, on_tile_ready=None):
        self._sat = sat_cache
        self._ovl = ovl_cache
        self._on_ready = on_tile_ready   # callback to trigger map redraw
        self._pending = set()            # tiles currently being fetched
        self._lock = threading.Lock()
        self._fail_count = 0
        self._offline = False            # circuit-breaker flag
        # 4 workers balances throughput vs CDN politeness
        self._pool = ThreadPoolExecutor(max_workers=4)

    def request(self, z, x, y):
        """Request satellite + overlay tile download."""
        key = (z, x, y)
        with self._lock:
            # Skip if already in-flight or network is down
            if key in self._pending or self._offline:
                return
            self._pending.add(key)
        self._pool.submit(self._fetch, z, x, y)

    def _fetch(self, z, x, y):
        ok = False
        try:
            # Download satellite tile (base imagery layer)
            if self._sat.get(z, x, y) is None:
                url = SATELLITE_URL.format(z=z, y=y, x=x)
                data = self._download(url)
                if data:
                    self._sat.put(z, x, y, data)
                    ok = True
            # Download overlay tile (roads/labels, transparent PNG)
            if self._ovl.get(z, x, y) is None:
                url = OVERLAY_URL.format(z=z, y=y, x=x)
                data = self._download(url)
                if data:
                    self._ovl.put(z, x, y, data)
                    ok = True

            # Success resets the consecutive failure counter
            with self._lock:
                self._fail_count = 0
            # Notify main thread to redraw with new tiles
            if ok and self._on_ready:
                Clock.schedule_once(lambda dt: self._on_ready(), 0)

        except Exception as exc:
            with self._lock:
                self._fail_count += 1
                count = self._fail_count
            # Log first few failures at warning level for visibility
            if count <= 3:
                log.warning("Tile download failed (%s/%s/%s): %s", z, x, y, exc)
            else:
                log.debug("Tile download failed (%s/%s/%s): %s", z, x, y, exc)
            # Circuit breaker: enter offline mode after too many failures
            if count >= 20:
                with self._lock:
                    self._offline = True
                log.warning("Too many tile failures (%d) — offline mode", count)
        finally:
            with self._lock:
                self._pending.discard((z, x, y))

    def reset_offline(self):
        """Allow tile downloads again after entering offline mode."""
        with self._lock:
            self._offline = False
            self._fail_count = 0
        log.info("Tile downloader reset — retrying downloads")

    @staticmethod
    def _download(url):
        req = urllib.request.Request(url, headers={
            "User-Agent": "CopterSonde-GCS/1.0",
        })
        resp = urllib.request.urlopen(req, timeout=10, context=_tile_ssl_ctx)
        return resp.read()
