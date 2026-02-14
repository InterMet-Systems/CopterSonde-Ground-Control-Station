"""
Canvas-drawn plot widgets for CopterSonde GCS.

TimeSeriesPlot  – rolling time-series with auto-scaling Y axis
ProfilePlot     – value vs altitude profile with auto-scaling axes
"""

from kivy.uix.widget import Widget
from kivy.graphics import Color, Rectangle, Line
from kivy.core.text import Label as CoreLabel
from kivy.properties import NumericProperty, StringProperty

from app.theme import get_color


class TimeSeriesPlot(Widget):
    """Canvas-drawn time-series plot with auto-scaling axes."""

    title = StringProperty('')
    y_label = StringProperty('')
    x_window = NumericProperty(30.0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
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
            Color(*get_color("bg_plot_widget"))
            Rectangle(pos=self.pos, size=self.size)

            ml, mr, mt, mb = 62, 12, 28, 30
            px = self.x + ml
            py = self.y + mb
            pw = w - ml - mr
            ph = h - mt - mb

            # Plot background
            Color(*get_color("bg_plot_area"))
            Rectangle(pos=(px, py), size=(pw, ph))
            Color(*get_color("plot_border"))
            Line(rectangle=(px, py, pw, ph), width=1)

            # Title
            tex = self._tex(self.title, 18, get_color("plot_title"), bold=True)
            self._draw_tex(tex, self.x + (w - tex.width) / 2,
                           self.y + h - mt + 2)

            # Collect all values/times for range computation
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

            # Y range with padding
            y_min, y_max = min(all_vals), max(all_vals)
            if y_max - y_min < 0.1:
                y_min -= 0.5
                y_max += 0.5
            pad = (y_max - y_min) * 0.08
            y_min -= pad
            y_max += pad * 2  # expand both directions

            # X range: fixed-width rolling window
            t_max = max(all_times)
            t_min = t_max - self.x_window
            t_range = self.x_window

            # Grid + Y axis labels
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

            # X axis labels (time)
            n_x = 4
            for i in range(n_x + 1):
                frac = i / n_x
                gx = px + frac * pw
                Line(points=[gx, py, gx, py + ph], width=0.5)
                tv = t_min + frac * t_range
                m, s = divmod(int(tv), 60)
                tex = self._tex(f"{m}:{s:02d}", 13, get_color("plot_x_label"))
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
                tex = self._tex(name, 18, color)
                leg_x -= tex.width + 18
                Color(*color)
                Line(points=[leg_x - 14, leg_y + tex.height / 2,
                             leg_x - 2, leg_y + tex.height / 2], width=2)
                self._draw_tex(tex, leg_x, leg_y)


class ProfilePlot(Widget):
    """Canvas-drawn value-vs-altitude profile plot.

    X axis = measured value, Y axis = altitude (m).
    """

    title = StringProperty('')
    x_label = StringProperty('')

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
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
            Color(*get_color("bg_plot_widget"))
            Rectangle(pos=self.pos, size=self.size)

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

            # Collect ranges
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
            Color(*get_color("plot_grid"))
            for i in range(n_y + 1):
                frac = i / n_y
                gy = py + frac * ph
                Line(points=[px, gy, px + pw, gy], width=0.5)
                alt = y_min + frac * y_range
                tex = self._tex(f"{alt:.0f}", 14, get_color("text_axis"))
                self._draw_tex(tex, px - tex.width - 3,
                               gy - tex.height / 2)

            # X axis (value) grid + labels
            n_x = 4
            for i in range(n_x + 1):
                frac = i / n_x
                gx = px + frac * pw
                Line(points=[gx, py, gx, py + ph], width=0.5)
                val = x_min + frac * x_range
                tex = self._tex(f"{val:.1f}", 13, get_color("plot_x_label"))
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
                tex = self._tex(name, 18, color)
                leg_x -= tex.width + 18
                Color(*color)
                Line(points=[leg_x - 14, leg_y + tex.height / 2,
                             leg_x - 2, leg_y + tex.height / 2], width=2)
                self._draw_tex(tex, leg_x, leg_y)
