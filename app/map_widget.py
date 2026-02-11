"""
Canvas-drawn map widget for CopterSonde GCS.

Shows drone position, flight track, ADS-B targets, and lat/lon grid.
Uses equirectangular projection (accurate at local scales).
"""

import math

from kivy.uix.widget import Widget
from kivy.graphics import (
    Color, Rectangle, Line, Ellipse,
    PushMatrix, PopMatrix, Rotate, Translate,
)
from kivy.core.text import Label as CoreLabel


class MapWidget(Widget):
    """Canvas-drawn map with drone position, track, and ADS-B overlay."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._lat = 0.0
        self._lon = 0.0
        self._heading = 0.0
        self._track = []       # [(lat, lon), ...]
        self._adsb = []        # [(callsign, lat, lon, alt_m, heading), ...]
        self._center_lat = 0.0
        self._center_lon = 0.0
        self._zoom = 0.008     # degrees of lat visible in half-height
        self._center_on_drone = True
        self._show_track = True
        self._show_adsb = True
        self.bind(pos=self._redraw, size=self._redraw)

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
        self._zoom = max(self._zoom * 0.6, 0.0005)
        self._redraw()

    def zoom_out(self):
        self._zoom = min(self._zoom * 1.6, 0.5)
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
    # Coordinate conversion
    # -----------------------------------------------------------------

    def _geo_to_px(self, lat, lon, w, h):
        """Convert lat/lon to pixel coordinates."""
        cx, cy = self.center_x, self.center_y
        # Latitude correction for longitude scaling
        cos_lat = math.cos(math.radians(self._center_lat)) or 0.01
        dx = (lon - self._center_lon) / self._zoom * (h / 2) * cos_lat
        dy = (lat - self._center_lat) / self._zoom * (h / 2)
        return cx + dx, cy + dy

    # -----------------------------------------------------------------
    # Drawing helpers
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
            # Dark background
            Color(0.06, 0.08, 0.1, 1)
            Rectangle(pos=self.pos, size=self.size)

            # Grid lines
            self._draw_grid(w, h)

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

    def _draw_grid(self, w, h):
        """Draw lat/lon grid lines."""
        # Choose grid spacing based on zoom
        for spacing in [0.1, 0.05, 0.02, 0.01, 0.005, 0.002, 0.001, 0.0005]:
            if self._zoom / spacing < 5:
                break

        Color(0.15, 0.18, 0.2, 0.6)
        clat, clon = self._center_lat, self._center_lon

        # Latitude lines
        lat_start = (clat - self._zoom * 2) // spacing * spacing
        lat_end = clat + self._zoom * 2
        lat = lat_start
        while lat <= lat_end:
            px1 = self._geo_to_px(lat, clon - self._zoom * 2, w, h)
            px2 = self._geo_to_px(lat, clon + self._zoom * 2, w, h)
            if self.y <= px1[1] <= self.y + h:
                Line(points=[self.x, px1[1], self.x + w, px1[1]], width=0.5)
                tex = self._tex(f"{lat:.4f}", 9, (0.3, 0.35, 0.4, 1))
                self._draw_tex(tex, self.x + 2, px1[1] + 2)
            lat += spacing

        # Longitude lines
        lon_start = (clon - self._zoom * 2) // spacing * spacing
        lon_end = clon + self._zoom * 2
        lon = lon_start
        while lon <= lon_end:
            px1 = self._geo_to_px(clat, lon, w, h)
            if self.x <= px1[0] <= self.x + w:
                Line(points=[px1[0], self.y, px1[0], self.y + h], width=0.5)
                tex = self._tex(f"{lon:.4f}", 9, (0.3, 0.35, 0.4, 1))
                self._draw_tex(tex, px1[0] + 2, self.y + 2)
            lon += spacing

    def _draw_track(self, w, h):
        """Draw flight track breadcrumbs."""
        Color(0.3, 0.7, 0.3, 0.7)
        pts = []
        for lat, lon in self._track:
            px, py = self._geo_to_px(lat, lon, w, h)
            pts.extend([px, py])
        if len(pts) >= 4:
            Line(points=pts, width=1.2)

    def _draw_adsb(self, w, h):
        """Draw ADS-B target markers."""
        for callsign, lat, lon, alt_m, hdg in self._adsb:
            px, py = self._geo_to_px(lat, lon, w, h)
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
            tex = self._tex(label, 9, (1, 0.7, 0.2, 0.9))
            self._draw_tex(tex, px + s + 3, py - tex.height / 2)

    def _draw_drone(self, w, h):
        """Draw drone position marker with heading indicator."""
        if self._lat == 0 and self._lon == 0:
            return
        px, py = self._geo_to_px(self._lat, self._lon, w, h)
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
        """Draw scale bar in bottom-right."""
        # Calculate scale: how many meters per degree of lat
        m_per_deg = 111320.0
        zoom_m = self._zoom * m_per_deg  # meters in half-height

        # Choose nice scale bar length
        bar_m = 100
        for candidate in [50, 100, 200, 500, 1000, 2000, 5000]:
            if candidate < zoom_m * 0.4:
                bar_m = candidate

        bar_px = bar_m / zoom_m * (h / 2)
        bx = self.x + w - bar_px - 20
        by = self.y + 20

        Color(1, 1, 1, 0.7)
        Line(points=[bx, by, bx + bar_px, by], width=1.5)
        Line(points=[bx, by - 3, bx, by + 3], width=1)
        Line(points=[bx + bar_px, by - 3, bx + bar_px, by + 3], width=1)

        label = f"{bar_m} m" if bar_m < 1000 else f"{bar_m/1000:.0f} km"
        tex = self._tex(label, 10, (1, 1, 1, 0.7))
        self._draw_tex(tex, bx + (bar_px - tex.width) / 2, by + 5)

    def _draw_info(self, w, h):
        """Draw position readout in top-left."""
        tex = self._tex(
            f"{self._lat:.5f}, {self._lon:.5f}  HDG {self._heading:.0f}\u00b0",
            11, (0.6, 0.7, 0.8, 1))
        self._draw_tex(tex, self.x + 4, self.y + h - tex.height - 4)
