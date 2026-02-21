"""
Canvas-drawn plot widgets for CopterSonde GCS.

TimeSeriesPlot  – rolling time-series with auto-scaling Y axis
ProfilePlot     – value vs altitude profile with auto-scaling axes
"""

from collections import OrderedDict

from kivy.uix.widget import Widget
from kivy.clock import Clock
from kivy.graphics import Color, Rectangle, Line
from kivy.core.text import Label as CoreLabel
from kivy.properties import NumericProperty, StringProperty

from app.theme import get_color

# Shared LRU cache limit for text textures (see _tex() methods below)
_TEX_CACHE_MAX = 150


class TimeSeriesPlot(Widget):
    """Canvas-drawn time-series plot with auto-scaling axes."""

    title = StringProperty('')
    y_label = StringProperty('')
    x_window = NumericProperty(30.0)  # rolling time window in seconds

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._series = {}  # name -> (rgba_tuple, [(t, val), ...])
        # Dirty-flag coalescing: batches multiple set_data() + resize events
        # into a single redraw on the next frame.
        self._dirty = True
        self._redraw_scheduled = False
        self._tex_cache = OrderedDict()  # LRU text texture cache
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

    def set_data(self, series_dict):
        """Update plot data.

        Args:
            series_dict: {name: (color_tuple, [(t, val), ...])}
        """
        self._series = series_dict
        self._mark_dirty()

    # -----------------------------------------------------------------
    # Drawing helpers — LRU text texture cache
    # -----------------------------------------------------------------
    # Same pattern as FlightHUD: cache CoreLabel rasterizations to avoid
    # expensive per-frame glyph rendering on the GPU.

    def _tex(self, text, font_size, color=(1, 1, 1, 1), bold=False):
        key = (str(text), int(font_size), tuple(color), bold)
        tex = self._tex_cache.get(key)
        if tex is not None:
            self._tex_cache.move_to_end(key)
            return tex
        lbl = CoreLabel(text=str(text), font_size=max(font_size, 8),
                        color=color, bold=bold)
        lbl.refresh()
        tex = lbl.texture
        self._tex_cache[key] = tex
        if len(self._tex_cache) > _TEX_CACHE_MAX:
            self._tex_cache.popitem(last=False)
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
        if w < 60 or h < 40:
            return

        with self.canvas:
            # Background
            Color(*get_color("bg_plot_widget"))
            Rectangle(pos=self.pos, size=self.size)

            # Margins: left (Y-axis labels), right, top (title), bottom (X-axis)
            ml, mr, mt, mb = 62, 12, 28, 30
            px = self.x + ml       # plot area origin X
            py = self.y + mb       # plot area origin Y
            pw = w - ml - mr       # plot area width
            ph = h - mt - mb       # plot area height

            # Plot background
            Color(*get_color("bg_plot_area"))
            Rectangle(pos=(px, py), size=(pw, ph))
            Color(*get_color("plot_border"))
            Line(rectangle=(px, py, pw, ph), width=1)

            # Title
            tex = self._tex(self.title, 18, get_color("plot_title"), bold=True)
            self._draw_tex(tex, self.x + (w - tex.width) / 2,
                           self.y + h - mt + 2)

            # ── Auto-scaling axis range computation ───────────────────
            # Gather all data points to determine min/max for both axes.
            all_vals, all_times = [], []
            for _name, (_, pts) in self._series.items():
                for t, v in pts:
                    all_vals.append(v)
                    all_times.append(t)

            if not all_vals:
                tex = self._tex("No data", 16, get_color("text_dim"))
                self._draw_tex(tex, px + (pw - tex.width) / 2,
                               py + (ph - tex.height) / 2)
                return

            # Y range with 8% padding so traces don't touch the edges
            y_min, y_max = min(all_vals), max(all_vals)
            if y_max - y_min < 0.1:
                # Avoid degenerate range when all values are nearly equal
                y_min -= 0.5
                y_max += 0.5
            pad = (y_max - y_min) * 0.08
            y_min -= pad
            y_max += pad * 2

            # X range: fixed-width rolling window anchored to latest time
            t_max = max(all_times)
            t_min = t_max - self.x_window
            t_range = self.x_window

            # ── Grid lines and Y axis labels ─────────────────────────
            n_ticks = 5
            Color(*get_color("plot_grid"))
            y_range = y_max - y_min
            for i in range(n_ticks + 1):
                frac = i / n_ticks
                gy = py + frac * ph
                Line(points=[px, gy, px + pw, gy], width=0.5)
                val = y_min + frac * y_range
                tex = self._tex(f"{val:.1f}", 14, get_color("text_axis"))
                self._draw_tex(tex, px - tex.width - 3,
                               gy - tex.height / 2)

            # ── X axis labels (elapsed time as M:SS) ─────────────────
            n_x = 4
            for i in range(n_x + 1):
                frac = i / n_x
                gx = px + frac * pw
                Line(points=[gx, py, gx, py + ph], width=0.5)
                tv = t_min + frac * t_range
                m, s = divmod(int(tv), 60)
                tex = self._tex(f"{m}:{s:02d}", 13, get_color("plot_x_label"))
                self._draw_tex(tex, gx - tex.width / 2, self.y + 2)

            # ── Draw each data series ─────────────────────────────────
            for _name, (color, pts) in self._series.items():
                if len(pts) < 2:
                    continue
                Color(*color)
                line_pts = []
                for t, v in pts:
                    if t < t_min:
                        continue
                    # Map (time, value) -> (pixel_x, pixel_y)
                    lx = px + (t - t_min) / t_range * pw
                    ly = py + (v - y_min) / y_range * ph
                    ly = max(py, min(py + ph, ly))  # clamp to plot area
                    line_pts.extend([lx, ly])
                if len(line_pts) >= 4:
                    Line(points=line_pts, width=1.2)

            # Legend (top-right inside plot area)
            leg_x = px + pw - 8
            leg_y = py + ph - 20
            for name, (color, _) in reversed(list(self._series.items())):
                tex = self._tex(name, 18, color)
                leg_x -= tex.width + 18
                Color(*color)
                Line(points=[leg_x - 14, leg_y + tex.height / 2,
                             leg_x - 2, leg_y + tex.height / 2], width=2)
                self._draw_tex(tex, leg_x, leg_y)


class ProfilePlot(Widget):
    """Canvas-drawn value-vs-altitude profile plot.

    X axis = measured value, Y axis = altitude (m).
    Unlike TimeSeriesPlot where X is time, here X is the sensor reading
    and Y is altitude — producing a classic atmospheric profile chart.
    """

    title = StringProperty('')
    x_label = StringProperty('')

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._series = {}  # name -> (color, [(value, altitude), ...])
        # Same dirty-flag + LRU cache pattern as TimeSeriesPlot
        self._dirty = True
        self._redraw_scheduled = False
        self._tex_cache = OrderedDict()
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

    def set_data(self, series_dict):
        """Update plot data.

        Args:
            series_dict: {name: (color_tuple, [(value, altitude), ...])}
        """
        self._series = series_dict
        self._mark_dirty()

    def _tex(self, text, font_size, color=(1, 1, 1, 1), bold=False):
        key = (str(text), int(font_size), tuple(color), bold)
        tex = self._tex_cache.get(key)
        if tex is not None:
            self._tex_cache.move_to_end(key)
            return tex
        lbl = CoreLabel(text=str(text), font_size=max(font_size, 8),
                        color=color, bold=bold)
        lbl.refresh()
        tex = lbl.texture
        self._tex_cache[key] = tex
        if len(self._tex_cache) > _TEX_CACHE_MAX:
            self._tex_cache.popitem(last=False)
        return tex

    def _draw_tex(self, tex, x, y):
        Color(1, 1, 1, 1)
        Rectangle(texture=tex, pos=(x, y), size=tex.size)

    def _redraw(self, *_args):
        self.canvas.clear()
        w, h = self.size
        if w < 60 or h < 40:
            return

        with self.canvas:
            Color(*get_color("bg_plot_widget"))
            Rectangle(pos=self.pos, size=self.size)

            # Margins: left (altitude labels), right, top (title), bottom (value labels)
            ml, mr, mt, mb = 56, 12, 28, 32
            px = self.x + ml
            py = self.y + mb
            pw = w - ml - mr
            ph = h - mt - mb

            Color(*get_color("bg_plot_area"))
            Rectangle(pos=(px, py), size=(pw, ph))
            Color(*get_color("plot_border"))
            Line(rectangle=(px, py, pw, ph), width=1)

            # Title
            tex = self._tex(self.title, 18, get_color("plot_title"), bold=True)
            self._draw_tex(tex, self.x + (w - tex.width) / 2,
                           self.y + h - mt + 2)

            # ── Auto-scaling range computation ────────────────────────
            all_vals, all_alts = [], []
            for _, (_, pts) in self._series.items():
                for v, a in pts:
                    all_vals.append(v)
                    all_alts.append(a)

            if not all_vals:
                tex = self._tex("No data", 16, get_color("text_dim"))
                self._draw_tex(tex, px + (pw - tex.width) / 2,
                               py + (ph - tex.height) / 2)
                return

            # X axis (sensor value) auto-range with padding
            x_min, x_max = min(all_vals), max(all_vals)
            if x_max - x_min < 0.1:
                x_min -= 0.5
                x_max += 0.5
            xpad = (x_max - x_min) * 0.08
            x_min -= xpad
            x_max += xpad
            x_range = x_max - x_min

            # Y axis (altitude) always starts at ground level (0 m)
            y_min = 0.0
            y_max = max(all_alts) * 1.1 if max(all_alts) > 1 else 10.0
            y_range = max(y_max - y_min, 1.0)

            # Y axis (altitude) grid + labels
            n_y = 5
            Color(*get_color("plot_grid"))
            for i in range(n_y + 1):
                frac = i / n_y
                gy = py + frac * ph
                Line(points=[px, gy, px + pw, gy], width=0.5)
                alt = y_min + frac * y_range
                tex = self._tex(f"{alt:.0f}", 14, get_color("text_axis"))
                self._draw_tex(tex, px - tex.width - 3,
                               gy - tex.height / 2)

            # X axis (measured value) grid + labels
            n_x = 4
            for i in range(n_x + 1):
                frac = i / n_x
                gx = px + frac * pw
                Line(points=[gx, py, gx, py + ph], width=0.5)
                val = x_min + frac * x_range
                tex = self._tex(f"{val:.1f}", 13, get_color("plot_x_label"))
                self._draw_tex(tex, gx - tex.width / 2, self.y + 2)

            # ── Draw series ───────────────────────────────────────────
            # Points are sorted by altitude so the line traces upward
            for name, (color, pts) in self._series.items():
                if len(pts) < 2:
                    continue
                sorted_pts = sorted(pts, key=lambda p: p[1])
                Color(*color)
                line_pts = []
                for v, a in sorted_pts:
                    # Map (value, altitude) -> (pixel_x, pixel_y)
                    lx = px + (v - x_min) / x_range * pw
                    ly = py + (a - y_min) / y_range * ph
                    lx = max(px, min(px + pw, lx))  # clamp to plot area
                    ly = max(py, min(py + ph, ly))
                    line_pts.extend([lx, ly])
                if len(line_pts) >= 4:
                    Line(points=line_pts, width=1.3)

            # Legend
            leg_x = px + pw - 8
            leg_y = py + ph - 20
            for name, (color, _) in reversed(list(self._series.items())):
                tex = self._tex(name, 18, color)
                leg_x -= tex.width + 18
                Color(*color)
                Line(points=[leg_x - 14, leg_y + tex.height / 2,
                             leg_x - 2, leg_y + tex.height / 2], width=2)
                self._draw_tex(tex, leg_x, leg_y)
