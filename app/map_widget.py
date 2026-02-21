"""
Canvas-drawn map widget with satellite tile background for CopterSonde GCS.

Uses Spherical Mercator (EPSG:3857) tiles from ArcGIS World Imagery
with road/label overlay for hybrid view.  Drone position, flight track,
ADS-B targets, and scale bar are drawn on top.
"""

import math
import os
from collections import OrderedDict
from io import BytesIO

from kivy.uix.widget import Widget
from kivy.clock import Clock
from kivy.graphics import Color, Rectangle, Line, Ellipse, Mesh
from kivy.core.text import Label as CoreLabel
from kivy.core.image import Image as CoreImage

from app.tile_manager import (
    TileCache, TileDownloader,
    lat_lon_to_pixel, TILE_SIZE, MIN_ZOOM, MAX_ZOOM, DEFAULT_ZOOM,
)
from app.theme import get_color
from gcs.logutil import get_logger

log = get_logger("map_widget")

_TEXT_CACHE_MAX = 150             # LRU text texture cache limit
_MAX_TRACK_DRAW_POINTS = 300     # downsample track beyond this for performance


def _cache_base():
    """Return a writable cache directory for map tiles."""
    try:
        from android.storage import app_storage_path  # type: ignore
        return os.path.join(app_storage_path(), "cache")
    except ImportError:
        return os.path.join(
            os.path.expanduser("~"), ".coptersonde_gcs", "cache")


class MapWidget(Widget):
    """Canvas-drawn map with satellite tiles, drone, track, ADS-B overlay."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Drone state
        self._lat = 0.0
        self._lon = 0.0
        self._heading = 0.0
        self._track = []       # [(lat, lon), ...] flight breadcrumbs
        self._adsb = []        # [(callsign, lat, lon, alt_m, heading), ...]
        # Map viewport center (may diverge from drone if centering is off)
        self._center_lat = 0.0
        self._center_lon = 0.0
        self._zoom = DEFAULT_ZOOM  # integer tile zoom level
        self._center_on_drone = True
        self._show_track = True
        self._show_adsb = True

        # ── Tile infrastructure ──────────────────────────────────────
        # Two separate caches for satellite imagery and road overlay,
        # each with their own memory LRU + disk persistence.
        base = _cache_base()
        self._sat_cache = TileCache(os.path.join(base, "sat_tiles"))
        self._ovl_cache = TileCache(os.path.join(base, "ovl_tiles"))
        self._downloader = TileDownloader(
            self._sat_cache, self._ovl_cache,
            on_tile_ready=self._on_tiles_ready,
        )
        # GPU texture cache — converts raw PNG/JPEG bytes to Kivy Textures
        self._tile_tex_cache = {}  # (layer, z, x, y) -> Kivy Texture
        self._text_cache = OrderedDict()  # LRU text texture cache

        # Dirty-flag coalescing pattern (same as HUD/Plot widgets)
        self._dirty = True
        self._redraw_scheduled = False
        self.bind(pos=self._mark_dirty, size=self._mark_dirty)

    def _mark_dirty(self, *_args):
        self._dirty = True
        if not self._redraw_scheduled:
            self._redraw_scheduled = True
            Clock.schedule_once(self._do_redraw, 0)

    def _do_redraw(self, _dt=None):
        self._redraw_scheduled = False
        if self._dirty:
            self._dirty = False
            self._redraw()

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def set_state(self, lat, lon, heading, track, adsb_targets):
        self._lat = lat
        self._lon = lon
        self._heading = heading
        self._track = track
        self._adsb = adsb_targets
        if self._center_on_drone and (lat != 0 or lon != 0):
            self._center_lat = lat
            self._center_lon = lon
        self._mark_dirty()

    def zoom_in(self):
        if self._zoom < MAX_ZOOM:
            self._zoom += 1
            self._mark_dirty()

    def zoom_out(self):
        if self._zoom > MIN_ZOOM:
            self._zoom -= 1
            self._mark_dirty()

    def toggle_center(self):
        self._center_on_drone = not self._center_on_drone

    def toggle_track(self):
        self._show_track = not self._show_track
        self._mark_dirty()
        return self._show_track

    def toggle_adsb(self):
        self._show_adsb = not self._show_adsb
        self._mark_dirty()
        return self._show_adsb

    # -----------------------------------------------------------------
    # Tile callback
    # -----------------------------------------------------------------

    def _on_tiles_ready(self):
        """Called on main thread when new tiles arrive."""
        self._mark_dirty()

    # -----------------------------------------------------------------
    # Coordinate conversion (Spherical Mercator -> Kivy pixels)
    # -----------------------------------------------------------------
    # Key insight: Mercator Y increases downward (north = 0) but Kivy Y
    # increases upward (bottom = 0).  We negate the Y delta to bridge
    # the two coordinate systems.

    def _geo_to_px(self, lat, lon):
        """Convert lat/lon to widget pixel coordinates."""
        cxg, cyg = lat_lon_to_pixel(
            self._center_lat, self._center_lon, self._zoom)
        pxg, pyg = lat_lon_to_pixel(lat, lon, self._zoom)
        # X: same direction in both systems (east = right)
        # Y: negate delta because Kivy Y-up vs Mercator Y-down
        return (self.center_x + (pxg - cxg),
                self.center_y - (pyg - cyg))

    # -----------------------------------------------------------------
    # Texture helpers
    # -----------------------------------------------------------------

    def _get_tile_tex(self, layer, z, x, y):
        """Get or create a Kivy texture for a cached tile.

        Converts raw image bytes from the tile cache into a GPU texture.
        Textures are cached to avoid repeated decoding of the same tile.
        """
        key = (layer, z, x, y)
        if key in self._tile_tex_cache:
            return self._tile_tex_cache[key]
        cache = self._sat_cache if layer == "sat" else self._ovl_cache
        data = cache.get(z, x, y)
        if data is None:
            return None
        try:
            # Detect image format from magic bytes (JPEG vs PNG)
            if data[:3] == b'\xff\xd8\xff':
                ext = "jpg"
            else:
                ext = "png"
            tex = CoreImage(BytesIO(data), ext=ext).texture
            self._tile_tex_cache[key] = tex
            # Evict oldest 100 textures when cache exceeds 400
            if len(self._tile_tex_cache) > 400:
                keys = list(self._tile_tex_cache.keys())
                for k in keys[:100]:
                    del self._tile_tex_cache[k]
            return tex
        except Exception:
            return None

    # -----------------------------------------------------------------
    # Text helpers (cached)
    # -----------------------------------------------------------------

    def _tex(self, text, font_size, color=(1, 1, 1, 1), bold=False):
        key = (str(text), int(font_size), tuple(color), bold)
        tex = self._text_cache.get(key)
        if tex is not None:
            self._text_cache.move_to_end(key)
            return tex
        lbl = CoreLabel(text=str(text), font_size=max(font_size, 8),
                        color=color, bold=bold)
        lbl.refresh()
        tex = lbl.texture
        self._text_cache[key] = tex
        if len(self._text_cache) > _TEXT_CACHE_MAX:
            self._text_cache.popitem(last=False)
        return tex

    def _draw_tex(self, tex, x, y):
        Color(1, 1, 1, 1)
        Rectangle(texture=tex, pos=(x, y), size=tex.size)

    # -----------------------------------------------------------------
    # Main draw
    # -----------------------------------------------------------------

    def _redraw(self, *_args):
        self.canvas.clear()
        w, h = self.size
        if w < 40 or h < 40:
            return

        with self.canvas:
            # Dark background (visible where tiles haven't loaded)
            Color(*get_color("bg_map"))
            Rectangle(pos=self.pos, size=self.size)

            # Satellite + overlay tiles
            self._draw_tiles(w, h)

            # Flight track
            if self._show_track and len(self._track) >= 2:
                self._draw_track(w, h)

            # ADS-B targets
            if self._show_adsb:
                self._draw_adsb(w, h)

            # Drone marker
            self._draw_drone(w, h)

            # Scale bar
            self._draw_scale(w, h)

            # Info overlay
            self._draw_info(w, h)

    def _draw_tiles(self, w, h):
        """Render visible satellite + overlay map tiles.

        Computes which tiles fall within the widget viewport, converts
        Mercator tile coordinates to Kivy widget coordinates, and draws
        them.  Missing tiles are requested from the downloader.
        """
        z = self._zoom
        cxg, cyg = lat_lon_to_pixel(
            self._center_lat, self._center_lon, z)

        # Visible area in global Mercator pixel coordinates
        left_g = cxg - w / 2
        right_g = cxg + w / 2
        top_g = cyg - h / 2      # Mercator: smaller Y = north
        bot_g = cyg + h / 2

        # Tile grid range covering viewport (with 1-tile buffer for smooth edges)
        tx_min = int(left_g // TILE_SIZE) - 1
        tx_max = int(right_g // TILE_SIZE) + 1
        ty_min = max(0, int(top_g // TILE_SIZE) - 1)
        ty_max = min(2 ** z - 1, int(bot_g // TILE_SIZE) + 1)
        max_t = 2 ** z  # total tiles in one row at this zoom

        for ty in range(ty_min, ty_max + 1):
            for tx in range(tx_min, tx_max + 1):
                # Wrap X for world-wrapping (tiles repeat past antimeridian)
                txw = tx % max_t
                if txw < 0:
                    txw += max_t

                # Tile NW corner in global Mercator pixels
                tile_gx = tx * TILE_SIZE
                tile_gy = ty * TILE_SIZE

                # Convert to Kivy widget coords (Y-up, tile origin is NW)
                sx = self.center_x + (tile_gx - cxg)
                # Subtract TILE_SIZE because tile origin is top-left but
                # Kivy Rectangle pos is bottom-left
                sy = self.center_y - (tile_gy - cyg) - TILE_SIZE

                # Satellite base layer
                sat_tex = self._get_tile_tex("sat", z, txw, ty)
                if sat_tex:
                    Color(1, 1, 1, 1)
                    Rectangle(texture=sat_tex, pos=(sx, sy),
                              size=(TILE_SIZE, TILE_SIZE))
                else:
                    # Dark placeholder while tile downloads in background
                    Color(*get_color("bg_map_loading"))
                    Rectangle(pos=(sx, sy), size=(TILE_SIZE, TILE_SIZE))
                    self._downloader.request(z, txw, ty)

                # Road/label overlay (transparent PNG composited on top)
                ovl_tex = self._get_tile_tex("ovl", z, txw, ty)
                if ovl_tex:
                    Color(1, 1, 1, 1)
                    Rectangle(texture=ovl_tex, pos=(sx, sy),
                              size=(TILE_SIZE, TILE_SIZE))

    def _draw_track(self, w, h):
        """Draw flight track breadcrumbs (downsampled for performance).

        Long flights can accumulate thousands of GPS points.  Drawing
        all of them would be slow, so we uniformly downsample to at most
        _MAX_TRACK_DRAW_POINTS while always keeping the latest point.
        """
        Color(*get_color("map_track"))
        track = self._track
        n = len(track)

        # Downsample: take every Nth point to stay within budget
        if n > _MAX_TRACK_DRAW_POINTS:
            stride = n / _MAX_TRACK_DRAW_POINTS
            indices = [int(i * stride) for i in range(_MAX_TRACK_DRAW_POINTS)]
            if indices[-1] != n - 1:
                indices.append(n - 1)  # always include latest point
        else:
            indices = range(n)

        pts = []
        for i in indices:
            lat, lon = track[i]
            px, py = self._geo_to_px(lat, lon)
            pts.extend([px, py])
        if len(pts) >= 4:
            Line(points=pts, width=3.6)

    def _draw_arrowhead(self, px, py, hdg_deg, size, rgba):
        """Draw a solid filled arrowhead at (px, py) pointing in hdg_deg.

        Uses Kivy's Mesh in triangle_fan mode for a GPU-filled triangle.
        sin(hdg)/cos(hdg) because heading 0 = north = +Y in screen space.
        """
        hdg = math.radians(hdg_deg)
        # Nose (front tip) — points in heading direction
        nx = px + math.sin(hdg) * size
        ny = py + math.cos(hdg) * size
        # Left rear wing — 140 degrees back from nose
        lx = px + math.sin(hdg + math.radians(140)) * size * 0.65
        ly = py + math.cos(hdg + math.radians(140)) * size * 0.65
        # Right rear wing — symmetric on the other side
        rx = px + math.sin(hdg - math.radians(140)) * size * 0.65
        ry = py + math.cos(hdg - math.radians(140)) * size * 0.65

        Color(*rgba)
        # Mesh vertices: [x, y, u, v] per vertex — u/v unused (no texture)
        Mesh(
            vertices=[nx, ny, 0, 0, lx, ly, 0, 0, rx, ry, 0, 0],
            indices=[0, 1, 2],
            mode='triangle_fan',
        )

    def _draw_adsb(self, w, h):
        """Draw ADS-B target markers as solid red arrowheads."""
        for callsign, lat, lon, alt_m, hdg in self._adsb:
            px, py = self._geo_to_px(lat, lon)
            if not (self.x - 60 <= px <= self.x + w + 60 and
                    self.y - 60 <= py <= self.y + h + 60):
                continue

            # Solid red arrowhead pointing in heading direction
            self._draw_arrowhead(px, py, hdg, 66, get_color("map_adsb"))

            # Label with background box
            alt_ft = alt_m * 3.281
            label = f"{callsign} {alt_ft:.0f}ft"
            tex = self._tex(label, 46, get_color("map_adsb_label"))
            lx = px + 36
            ly = py - tex.height / 2
            Color(*get_color("map_adsb_label_bg"))
            Rectangle(pos=(lx - 3, ly - 2),
                      size=(tex.width + 6, tex.height + 4))
            self._draw_tex(tex, lx, ly)

    def _draw_drone(self, w, h):
        """Draw drone position as a solid light green arrowhead."""
        if self._lat == 0 and self._lon == 0:
            return
        px, py = self._geo_to_px(self._lat, self._lon)

        # Solid light green arrowhead pointing in heading direction
        self._draw_arrowhead(px, py, self._heading, 87, get_color("map_drone"))

    def _draw_scale(self, w, h):
        """Draw scale bar in bottom-right with background."""
        # Ground resolution formula: at the equator zoom 0 covers the full
        # earth circumference in 256 px, so m/px = C_earth / 256 / 2^zoom
        # adjusted by cos(lat) for latitude convergence.
        m_per_px = (156543.03392
                    * math.cos(math.radians(self._center_lat))
                    / (2 ** self._zoom))

        bar_m = 100
        for candidate in [50, 100, 200, 500, 1000, 2000, 5000]:
            px_len = candidate / max(m_per_px, 0.001)
            if 40 < px_len < w * 0.3:
                bar_m = candidate

        bar_px = bar_m / max(m_per_px, 0.001)
        bx = self.x + w - bar_px - 20
        by = self.y + 20

        # Background for readability over imagery
        Color(*get_color("bg_overlay"))
        Rectangle(pos=(bx - 4, by - 8), size=(bar_px + 8, 70))

        Color(*get_color("map_scale"))
        Line(points=[bx, by, bx + bar_px, by], width=1.5)
        Line(points=[bx, by - 3, bx, by + 3], width=1)
        Line(points=[bx + bar_px, by - 3, bx + bar_px, by + 3], width=1)

        label = f"{bar_m} m" if bar_m < 1000 else f"{bar_m/1000:.0f} km"
        tex = self._tex(label, 50, get_color("map_scale"))
        self._draw_tex(tex, bx + (bar_px - tex.width) / 2, by + 8)

    def _draw_info(self, w, h):
        """Draw position readout + zoom in top-left with background."""
        info = (f"{self._lat:.5f}, {self._lon:.5f}  "
                f"HDG {self._heading:.0f}\u00b0  Z{self._zoom}")
        tex = self._tex(info, 54, get_color("map_info"))
        Color(*get_color("bg_overlay"))
        Rectangle(pos=(self.x + 2, self.y + h - tex.height - 6),
                  size=(tex.width + 8, tex.height + 4))
        self._draw_tex(tex, self.x + 6, self.y + h - tex.height - 4)
