"""
Slippy-map tile manager for CopterSonde GCS.

Downloads, caches, and serves satellite + hybrid overlay tiles
from ArcGIS World Imagery.  Uses Spherical Mercator (EPSG:3857).
"""

import math
import os
import threading
import urllib.request
from collections import OrderedDict

from kivy.clock import Clock

from gcs.logutil import get_logger

log = get_logger("tile_manager")

TILE_SIZE = 256
MIN_ZOOM = 1
MAX_ZOOM = 19
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

_MAX_LAT = 85.05112878  # Mercator latitude limit


# ── Mercator projection helpers ─────────────────────────────────────


def lat_lon_to_pixel(lat, lon, zoom):
    """Convert lat/lon to global Mercator pixel coordinates."""
    n = 2.0 ** zoom
    px = (lon + 180.0) / 360.0 * n * TILE_SIZE
    lat_c = max(-_MAX_LAT, min(_MAX_LAT, lat))
    lat_rad = math.radians(lat_c)
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
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
    return lat, lon


# ── Tile cache (memory LRU + disk) ──────────────────────────────────


class TileCache:
    """Two-level tile cache: in-memory LRU + persistent disk storage."""

    def __init__(self, cache_dir, max_memory=200):
        self._dir = cache_dir
        self._max = max_memory
        self._mem = OrderedDict()
        self._lock = threading.Lock()
        os.makedirs(cache_dir, exist_ok=True)

    def _disk_path(self, z, x, y):
        return os.path.join(self._dir, str(z), f"{x}_{y}.png")

    def get(self, z, x, y):
        """Return tile bytes or None."""
        key = (z, x, y)
        with self._lock:
            if key in self._mem:
                self._mem.move_to_end(key)
                return self._mem[key]
        path = self._disk_path(z, x, y)
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    data = f.read()
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
        path = self._disk_path(z, x, y)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        except OSError:
            pass

    def _evict(self):
        while len(self._mem) > self._max:
            self._mem.popitem(last=False)


# ── Tile downloader (threaded) ──────────────────────────────────────


class TileDownloader:
    """Background tile downloader with per-tile threads."""

    def __init__(self, sat_cache, ovl_cache, on_tile_ready=None):
        self._sat = sat_cache
        self._ovl = ovl_cache
        self._on_ready = on_tile_ready
        self._pending = set()
        self._lock = threading.Lock()
        self._fail_count = 0
        self._offline = False

    def request(self, z, x, y):
        """Request satellite + overlay tile download."""
        key = (z, x, y)
        with self._lock:
            if key in self._pending or self._offline:
                return
            self._pending.add(key)
        t = threading.Thread(target=self._fetch, args=(z, x, y),
                             daemon=True, name=f"tile-{z}-{x}-{y}")
        t.start()

    def _fetch(self, z, x, y):
        ok = False
        try:
            # Download satellite tile
            if self._sat.get(z, x, y) is None:
                url = SATELLITE_URL.format(z=z, y=y, x=x)
                data = self._download(url)
                if data:
                    self._sat.put(z, x, y, data)
                    ok = True
            # Download overlay tile
            if self._ovl.get(z, x, y) is None:
                url = OVERLAY_URL.format(z=z, y=y, x=x)
                data = self._download(url)
                if data:
                    self._ovl.put(z, x, y, data)
                    ok = True

            with self._lock:
                self._fail_count = 0
            if ok and self._on_ready:
                Clock.schedule_once(lambda dt: self._on_ready(), 0)

        except Exception as exc:
            log.debug("Tile download failed (%s/%s/%s): %s", z, x, y, exc)
            with self._lock:
                self._fail_count += 1
                if self._fail_count >= 5:
                    self._offline = True
                    log.warning("Too many tile failures — offline mode")
        finally:
            with self._lock:
                self._pending.discard((z, x, y))

    @staticmethod
    def _download(url):
        req = urllib.request.Request(url, headers={
            "User-Agent": "CopterSonde-GCS/1.0",
        })
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.read()
