"""
Canvas-drawn map widget with satellite tile background for CopterSonde GCS.

Uses Spherical Mercator (EPSG:3857) tiles from ArcGIS World Imagery
with road/label overlay for hybrid view.  Drone position, flight track,
ADS-B targets, and scale bar are drawn on top.
"""

import math
import os
from io import BytesIO

from kivy.uix.widget import Widget
from kivy.graphics import Color, Rectangle, Line, Ellipse
from kivy.core.text import Label as CoreLabel
from kivy.core.image import Image as CoreImage

from app.tile_manager import (
    TileCache, TileDownloader,
    lat_lon_to_pixel, TILE_SIZE, MIN_ZOOM, MAX_ZOOM, DEFAULT_ZOOM,
)
from gcs.logutil import get_logger

log = get_logger("map_widget")


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
        self._lat = 0.0
        self._lon = 0.0
        self._heading = 0.0
        self._track = []       # [(lat, lon), ...]
        self._adsb = []        # [(callsign, lat, lon, alt_m, heading), ...]
        self._center_lat = 0.0
        self._center_lon = 0.0
        self._zoom = DEFAULT_ZOOM  # integer tile zoom level
        self._center_on_drone = True
        self._show_track = True
        self._show_adsb = True

        # Tile infrastructure
        base = _cache_base()
        self._sat_cache = TileCache(os.path.join(base, "sat_tiles"))
        self._ovl_cache = TileCache(os.path.join(base, "ovl_tiles"))
        self._downloader = TileDownloader(
            self._sat_cache, self._ovl_cache,
            on_tile_ready=self._on_tiles_ready,
        )
        self._tex_cache = {}  # (layer, z, x, y) -> Kivy Texture

        self.bind(pos=self._redraw, size=self._redraw)

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
        self._redraw()

    def zoom_in(self):
        if self._zoom < MAX_ZOOM:
            self._zoom += 1
            self._redraw()

    def zoom_out(self):
        if self._zoom > MIN_ZOOM:
            self._zoom -= 1
            self._redraw()

    def toggle_center(self):
        self._center_on_drone = not self._center_on_drone

    def toggle_track(self):
        self._show_track = not self._show_track
        self._redraw()

    def toggle_adsb(self):
        self._show_adsb = not self._show_adsb
        self._redraw()

    # -----------------------------------------------------------------
    # Tile callback
    # -----------------------------------------------------------------

    def _on_tiles_ready(self):
        """Called on main thread when new tiles arrive."""
        self._redraw()

    # -----------------------------------------------------------------
    # Coordinate conversion (Spherical Mercator)
    # -----------------------------------------------------------------

    def _geo_to_px(self, lat, lon):
        """Convert lat/lon to widget pixel coordinates."""
        cxg, cyg = lat_lon_to_pixel(
            self._center_lat, self._center_lon, self._zoom)
        pxg, pyg = lat_lon_to_pixel(lat, lon, self._zoom)
        # Kivy Y increases upward; Mercator Y increases downward
        return (self.center_x + (pxg - cxg),
                self.center_y - (pyg - cyg))

    # -----------------------------------------------------------------
    # Texture helpers
    # -----------------------------------------------------------------

    def _get_tile_tex(self, layer, z, x, y):
        """Get or create a Kivy texture for a cached tile."""
        key = (layer, z, x, y)
        if key in self._tex_cache:
            return self._tex_cache[key]
        cache = self._sat_cache if layer == "sat" else self._ovl_cache
        data = cache.get(z, x, y)
        if data is None:
            return None
        try:
            # Detect image format from magic bytes
            if data[:3] == b'\xff\xd8\xff':
                ext = "jpg"
            else:
                ext = "png"
            tex = CoreImage(BytesIO(data), ext=ext).texture
            self._tex_cache[key] = tex
            # Limit texture cache size
            if len(self._tex_cache) > 400:
                keys = list(self._tex_cache.keys())
                for k in keys[:100]:
                    del self._tex_cache[k]
            return tex
        except Exception:
            return None

    # -----------------------------------------------------------------
    # Text helpers
    # -----------------------------------------------------------------

    def _tex(self, text, font_size, color=(1, 1, 1, 1), bold=False):
        lbl = CoreLabel(text=str(text), font_size=max(font_size, 8),
                        color=color, bold=bold)
        lbl.refresh()
        return lbl.texture

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
            Color(0.06, 0.08, 0.1, 1)
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
        """Render visible satellite + overlay map tiles."""
        z = self._zoom
        cxg, cyg = lat_lon_to_pixel(
            self._center_lat, self._center_lon, z)

        # Visible area in global pixel coordinates
        left_g = cxg - w / 2
        right_g = cxg + w / 2
        top_g = cyg - h / 2      # Mercator: smaller Y = north
        bot_g = cyg + h / 2

        # Tile index range (with 1-tile buffer)
        tx_min = int(left_g // TILE_SIZE) - 1
        tx_max = int(right_g // TILE_SIZE) + 1
        ty_min = max(0, int(top_g // TILE_SIZE) - 1)
        ty_max = min(2 ** z - 1, int(bot_g // TILE_SIZE) + 1)
        max_t = 2 ** z

        for ty in range(ty_min, ty_max + 1):
            for tx in range(tx_min, tx_max + 1):
                txw = tx % max_t
                if txw < 0:
                    txw += max_t

                # Tile NW corner in global pixels
                tile_gx = tx * TILE_SIZE
                tile_gy = ty * TILE_SIZE

                # Widget position (Kivy: Y-up, tile origin is NW = top-left)
                sx = self.center_x + (tile_gx - cxg)
                sy = self.center_y - (tile_gy - cyg) - TILE_SIZE

                # Satellite tile
                sat_tex = self._get_tile_tex("sat", z, txw, ty)
                if sat_tex:
                    Color(1, 1, 1, 1)
                    Rectangle(texture=sat_tex, pos=(sx, sy),
                              size=(TILE_SIZE, TILE_SIZE))
                else:
                    # Dark placeholder while downloading
                    Color(0.1, 0.12, 0.14, 1)
                    Rectangle(pos=(sx, sy), size=(TILE_SIZE, TILE_SIZE))
                    self._downloader.request(z, txw, ty)

                # Overlay tile (roads / labels — transparent PNG)
                ovl_tex = self._get_tile_tex("ovl", z, txw, ty)
                if ovl_tex:
                    Color(1, 1, 1, 1)
                    Rectangle(texture=ovl_tex, pos=(sx, sy),
                              size=(TILE_SIZE, TILE_SIZE))

    def _draw_track(self, w, h):
        """Draw flight track breadcrumbs."""
        Color(0.3, 0.7, 0.3, 0.7)
        pts = []
        for lat, lon in self._track:
            px, py = self._geo_to_px(lat, lon)
            pts.extend([px, py])
        if len(pts) >= 4:
            Line(points=pts, width=1.2)

    def _draw_adsb(self, w, h):
        """Draw ADS-B target markers."""
        for callsign, lat, lon, alt_m, hdg in self._adsb:
            px, py = self._geo_to_px(lat, lon)
            if not (self.x - 20 <= px <= self.x + w + 20 and
                    self.y - 20 <= py <= self.y + h + 20):
                continue

            # Diamond marker
            s = 6
            Color(1, 0.6, 0.1, 0.9)
            Line(points=[px, py + s, px + s, py, px, py - s, px - s, py],
                 width=1.2, close=True)

            # Label
            alt_ft = alt_m * 3.281
            label = f"{callsign} {alt_ft:.0f}ft"
            tex = self._tex(label, 13, (1, 0.7, 0.2, 0.9))
            self._draw_tex(tex, px + s + 3, py - tex.height / 2)

    def _draw_drone(self, w, h):
        """Draw drone position marker with heading indicator."""
        if self._lat == 0 and self._lon == 0:
            return
        px, py = self._geo_to_px(self._lat, self._lon)
        r = 8

        # Heading line
        hdg_rad = math.radians(self._heading)
        hx = px + math.sin(hdg_rad) * r * 2.5
        hy = py + math.cos(hdg_rad) * r * 2.5
        Color(0, 0.9, 0.4, 0.8)
        Line(points=[px, py, hx, hy], width=1.5)

        # Drone circle
        Color(0, 0.85, 0.4, 1)
        Line(circle=(px, py, r), width=2)

        # Center dot
        Color(0, 1, 0.5, 1)
        Ellipse(pos=(px - 3, py - 3), size=(6, 6))

    def _draw_scale(self, w, h):
        """Draw scale bar in bottom-right with background."""
        # Meters per pixel at current zoom and center latitude
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
        Color(0, 0, 0, 0.5)
        Rectangle(pos=(bx - 4, by - 8), size=(bar_px + 8, 28))

        Color(1, 1, 1, 0.9)
        Line(points=[bx, by, bx + bar_px, by], width=1.5)
        Line(points=[bx, by - 3, bx, by + 3], width=1)
        Line(points=[bx + bar_px, by - 3, bx + bar_px, by + 3], width=1)

        label = f"{bar_m} m" if bar_m < 1000 else f"{bar_m/1000:.0f} km"
        tex = self._tex(label, 14, (1, 1, 1, 0.9))
        self._draw_tex(tex, bx + (bar_px - tex.width) / 2, by + 5)

    def _draw_info(self, w, h):
        """Draw position readout + zoom in top-left with background."""
        info = (f"{self._lat:.5f}, {self._lon:.5f}  "
                f"HDG {self._heading:.0f}\u00b0  Z{self._zoom}")
        tex = self._tex(info, 15, (0.9, 0.95, 1, 1))
        Color(0, 0, 0, 0.5)
        Rectangle(pos=(self.x + 2, self.y + h - tex.height - 6),
                  size=(tex.width + 8, tex.height + 4))
        self._draw_tex(tex, self.x + 6, self.y + h - tex.height - 4)
