"""
Canvas-drawn plot widgets for CopterSonde GCS.

TimeSeriesPlot  – rolling time-series with auto-scaling Y axis
ProfilePlot     – value vs altitude profile with auto-scaling axes
"""

from kivy.uix.widget import Widget
from kivy.graphics import Color, Rectangle, Line
from kivy.core.text import Label as CoreLabel


class TimeSeriesPlot(Widget):
    """Canvas-drawn time-series plot with auto-scaling axes."""

    def __init__(self, title='', y_label='', x_window=120.0, **kwargs):
        super().__init__(**kwargs)
        self._title = title
        self._y_label = y_label
        self._x_window = x_window  # seconds of history to display
        self._series = {}  # name -> (rgba_tuple, [(t, val), ...])
        self.bind(pos=self._redraw, size=self._redraw)

    def set_data(self, series_dict):
        """Update plot data.

        Args:
            series_dict: {name: (color_tuple, [(t, val), ...])}
        """
        self._series = series_dict
        self._redraw()

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
        if w < 60 or h < 40:
            return

        with self.canvas:
            # Background
            Color(0.1, 0.1, 0.12, 1)
            Rectangle(pos=self.pos, size=self.size)

            ml, mr, mt, mb = 62, 12, 28, 30
            px = self.x + ml
            py = self.y + mb
            pw = w - ml - mr
            ph = h - mt - mb

            # Plot background
            Color(0.06, 0.06, 0.08, 1)
            Rectangle(pos=(px, py), size=(pw, ph))
            Color(0.3, 0.3, 0.35, 1)
            Line(rectangle=(px, py, pw, ph), width=1)

            # Title
            tex = self._tex(self._title, 18, (0.7, 0.75, 0.8, 1), bold=True)
            self._draw_tex(tex, self.x + (w - tex.width) / 2,
                           self.y + h - mt + 2)

            # Collect all values/times for range computation
            all_vals, all_times = [], []
            for _name, (_, pts) in self._series.items():
                for t, v in pts:
                    all_vals.append(v)
                    all_times.append(t)

            if not all_vals:
                tex = self._tex("No data", 16, (0.4, 0.4, 0.4, 1))
                self._draw_tex(tex, px + (pw - tex.width) / 2,
                               py + (ph - tex.height) / 2)
                return

            # Y range with padding
            y_min, y_max = min(all_vals), max(all_vals)
            if y_max - y_min < 0.1:
                y_min -= 0.5
                y_max += 0.5
            pad = (y_max - y_min) * 0.08
            y_min -= pad
            y_max -= -pad  # intentional: expand both directions
            y_max += pad

            # X range (rolling window)
            t_max = max(all_times)
            t_min = max(t_max - self._x_window, 0)
            t_range = max(t_max - t_min, 0.1)

            # Grid + Y axis labels
            n_ticks = 5
            Color(0.18, 0.18, 0.2, 1)
            y_range = y_max - y_min
            for i in range(n_ticks + 1):
                frac = i / n_ticks
                gy = py + frac * ph
                Line(points=[px, gy, px + pw, gy], width=0.5)
                val = y_min + frac * y_range
                tex = self._tex(f"{val:.1f}", 14, (0.5, 0.5, 0.5, 1))
                self._draw_tex(tex, px - tex.width - 3,
                               gy - tex.height / 2)

            # X axis labels (time)
            n_x = 4
            for i in range(n_x + 1):
                frac = i / n_x
                gx = px + frac * pw
                Line(points=[gx, py, gx, py + ph], width=0.5)
                tv = t_min + frac * t_range
                m, s = divmod(int(tv), 60)
                tex = self._tex(f"{m}:{s:02d}", 13, (0.45, 0.45, 0.45, 1))
                self._draw_tex(tex, gx - tex.width / 2, self.y + 2)

            # Draw each series
            for _name, (color, pts) in self._series.items():
                if len(pts) < 2:
                    continue
                Color(*color)
                line_pts = []
                for t, v in pts:
                    if t < t_min:
                        continue
                    lx = px + (t - t_min) / t_range * pw
                    ly = py + (v - y_min) / y_range * ph
                    ly = max(py, min(py + ph, ly))
                    line_pts.extend([lx, ly])
                if len(line_pts) >= 4:
                    Line(points=line_pts, width=1.2)

            # Legend (top-right inside plot)
            leg_x = px + pw - 8
            leg_y = py + ph - 20
            for name, (color, _) in reversed(list(self._series.items())):
                tex = self._tex(name, 14, color)
                leg_x -= tex.width + 18
                Color(*color)
                Line(points=[leg_x - 14, leg_y + tex.height / 2,
                             leg_x - 2, leg_y + tex.height / 2], width=2)
                self._draw_tex(tex, leg_x, leg_y)


class ProfilePlot(Widget):
    """Canvas-drawn value-vs-altitude profile plot.

    X axis = measured value, Y axis = altitude (m).
    """

    def __init__(self, title='', x_label='', **kwargs):
        super().__init__(**kwargs)
        self._title = title
        self._x_label = x_label
        self._series = {}  # name -> (color, [(value, altitude), ...])
        self.bind(pos=self._redraw, size=self._redraw)

    def set_data(self, series_dict):
        """Update plot data.

        Args:
            series_dict: {name: (color_tuple, [(value, altitude), ...])}
        """
        self._series = series_dict
        self._redraw()

    def _tex(self, text, font_size, color=(1, 1, 1, 1), bold=False):
        lbl = CoreLabel(text=str(text), font_size=max(font_size, 8),
                        color=color, bold=bold)
        lbl.refresh()
        return lbl.texture

    def _draw_tex(self, tex, x, y):
        Color(1, 1, 1, 1)
        Rectangle(texture=tex, pos=(x, y), size=tex.size)

    def _redraw(self, *_args):
        self.canvas.clear()
        w, h = self.size
        if w < 60 or h < 40:
            return

        with self.canvas:
            Color(0.1, 0.1, 0.12, 1)
            Rectangle(pos=self.pos, size=self.size)

            ml, mr, mt, mb = 56, 12, 28, 32
            px = self.x + ml
            py = self.y + mb
            pw = w - ml - mr
            ph = h - mt - mb

            Color(0.06, 0.06, 0.08, 1)
            Rectangle(pos=(px, py), size=(pw, ph))
            Color(0.3, 0.3, 0.35, 1)
            Line(rectangle=(px, py, pw, ph), width=1)

            # Title
            tex = self._tex(self._title, 18, (0.7, 0.75, 0.8, 1), bold=True)
            self._draw_tex(tex, self.x + (w - tex.width) / 2,
                           self.y + h - mt + 2)

            # Collect ranges
            all_vals, all_alts = [], []
            for _, (_, pts) in self._series.items():
                for v, a in pts:
                    all_vals.append(v)
                    all_alts.append(a)

            if not all_vals:
                tex = self._tex("No data", 16, (0.4, 0.4, 0.4, 1))
                self._draw_tex(tex, px + (pw - tex.width) / 2,
                               py + (ph - tex.height) / 2)
                return

            x_min, x_max = min(all_vals), max(all_vals)
            if x_max - x_min < 0.1:
                x_min -= 0.5
                x_max += 0.5
            xpad = (x_max - x_min) * 0.08
            x_min -= xpad
            x_max += xpad
            x_range = x_max - x_min

            y_min = 0.0
            y_max = max(all_alts) * 1.1 if max(all_alts) > 1 else 10.0
            y_range = max(y_max - y_min, 1.0)

            # Y axis (altitude) grid + labels
            n_y = 5
            Color(0.18, 0.18, 0.2, 1)
            for i in range(n_y + 1):
                frac = i / n_y
                gy = py + frac * ph
                Line(points=[px, gy, px + pw, gy], width=0.5)
                alt = y_min + frac * y_range
                tex = self._tex(f"{alt:.0f}", 14, (0.5, 0.5, 0.5, 1))
                self._draw_tex(tex, px - tex.width - 3,
                               gy - tex.height / 2)

            # X axis (value) grid + labels
            n_x = 4
            for i in range(n_x + 1):
                frac = i / n_x
                gx = px + frac * pw
                Line(points=[gx, py, gx, py + ph], width=0.5)
                val = x_min + frac * x_range
                tex = self._tex(f"{val:.1f}", 13, (0.45, 0.45, 0.45, 1))
                self._draw_tex(tex, gx - tex.width / 2, self.y + 2)

            # Draw series (sorted by altitude)
            for name, (color, pts) in self._series.items():
                if len(pts) < 2:
                    continue
                sorted_pts = sorted(pts, key=lambda p: p[1])
                Color(*color)
                line_pts = []
                for v, a in sorted_pts:
                    lx = px + (v - x_min) / x_range * pw
                    ly = py + (a - y_min) / y_range * ph
                    lx = max(px, min(px + pw, lx))
                    ly = max(py, min(py + ph, ly))
                    line_pts.extend([lx, ly])
                if len(line_pts) >= 4:
                    Line(points=line_pts, width=1.3)

            # Legend
            leg_x = px + pw - 8
            leg_y = py + ph - 20
            for name, (color, _) in reversed(list(self._series.items())):
                tex = self._tex(name, 14, color)
                leg_x -= tex.width + 18
                Color(*color)
                Line(points=[leg_x - 14, leg_y + tex.height / 2,
                             leg_x - 2, leg_y + tex.height / 2], width=2)
                self._draw_tex(tex, leg_x, leg_y)
