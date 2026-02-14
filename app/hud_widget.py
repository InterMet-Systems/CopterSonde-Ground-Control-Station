"""
Canvas-drawn Flight HUD widget for CopterSonde GCS.

Displays attitude indicator (artificial horizon with roll/pitch),
heading compass strip, speed and altitude tapes, and bottom info bar.
"""

import math

from kivy.uix.widget import Widget
from kivy.graphics import (
    Color, Rectangle, Line,
    PushMatrix, PopMatrix, Rotate, Translate,
    StencilPush, StencilPop, StencilUse, StencilUnUse,
)
from kivy.core.text import Label as CoreLabel

from app.theme import get_color


class FlightHUD(Widget):
    """Custom canvas-drawn flight HUD."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._roll = 0.0
        self._pitch = 0.0
        self._heading = 0.0
        self._airspeed = 0.0
        self._groundspeed = 0.0
        self._alt_rel = 0.0
        self._vz = 0.0
        self._throttle = 0
        self.bind(pos=self._redraw, size=self._redraw)

    def set_state(self, roll, pitch, heading, airspeed, groundspeed,
                  alt_rel, vz, throttle):
        self._roll = roll
        self._pitch = pitch
        self._heading = heading
        self._airspeed = airspeed
        self._groundspeed = groundspeed
        self._alt_rel = alt_rel
        self._vz = vz
        self._throttle = throttle
        self._redraw()

    # -----------------------------------------------------------------
    # Main draw
    # -----------------------------------------------------------------

    def _redraw(self, *_args):
        self.canvas.clear()
        w, h = self.size
        if w < 20 or h < 20:
            return

        with self.canvas:
            Color(*get_color("bg_hud"))
            Rectangle(pos=self.pos, size=self.size)

            hdg_h = max(h * 0.10, 30)
            info_h = max(h * 0.08, 24)
            tape_w = max(w * 0.14, 50)
            gap = 3

            ai_x = self.x + tape_w + gap
            ai_y = self.y + info_h + gap
            ai_w = w - 2 * tape_w - 2 * gap
            ai_h = h - hdg_h - info_h - 2 * gap

            self._draw_attitude(ai_x, ai_y, ai_w, ai_h)
            self._draw_heading(self.x, self.y + h - hdg_h, w, hdg_h)
            self._draw_speed_tape(self.x, ai_y, tape_w, ai_h)
            self._draw_alt_tape(self.x + w - tape_w, ai_y, tape_w, ai_h)
            self._draw_info_bar(self.x, self.y, w, info_h)

    # -----------------------------------------------------------------
    # Helpers
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
    # Attitude indicator
    # -----------------------------------------------------------------

    def _draw_attitude(self, x, y, w, h):
        cx, cy = x + w / 2, y + h / 2
        pitch_scale = h / 40.0
        pitch_px = -math.degrees(self._pitch) * pitch_scale
        roll_deg = math.degrees(self._roll)

        # Clip to AI area
        StencilPush()
        Rectangle(pos=(x, y), size=(w, h))
        StencilUse()

        PushMatrix()
        Translate(cx, cy, 0)
        Rotate(angle=roll_deg, axis=(0, 0, 1))

        # Sky
        Color(*get_color("hud_sky"))
        Rectangle(pos=(-w * 1.5, pitch_px), size=(w * 3, h * 3))

        # Ground
        Color(*get_color("hud_ground"))
        Rectangle(pos=(-w * 1.5, pitch_px - h * 3), size=(w * 3, h * 3))

        # Horizon line
        Color(*get_color("hud_horizon"))
        Line(points=[-w * 1.5, pitch_px, w * 1.5, pitch_px], width=1.5)

        # Pitch ladder (every 5 degrees)
        Color(*get_color("hud_pitch_ladder"))
        for deg in range(-20, 25, 5):
            if deg == 0:
                continue
            py = pitch_px + deg * pitch_scale
            lw = w * 0.12 if deg % 10 == 0 else w * 0.06
            Line(points=[-lw, py, lw, py], width=1)

        PopMatrix()

        StencilUnUse()
        Rectangle(pos=(x, y), size=(w, h))
        StencilPop()

        # Border
        Color(*get_color("hud_border"))
        Line(rectangle=(x, y, w, h), width=1.2)

        # Fixed center crosshair
        cw = w * 0.07
        Color(*get_color("hud_crosshair"))
        Line(points=[cx - cw, cy, cx - cw * 0.3, cy], width=2)
        Line(points=[cx + cw * 0.3, cy, cx + cw, cy], width=2)
        Line(points=[cx, cy - cw * 0.5, cx, cy], width=2)

        # Roll readout
        tex = self._tex(f"R {roll_deg:+.1f}\u00b0",
                        h * 0.06, get_color("hud_roll_readout"))
        self._draw_tex(tex, x + 4, y + h - tex.height - 4)

    # -----------------------------------------------------------------
    # Heading compass
    # -----------------------------------------------------------------

    def _draw_heading(self, x, y, w, h):
        Color(*get_color("bg_hud"))
        Rectangle(pos=(x, y), size=(w, h))

        heading = self._heading
        px_per_deg = w / 90.0
        cx = x + w / 2

        start = int(heading) - 50
        Color(*get_color("hud_heading_tick"))
        for deg_raw in range(start, start + 101):
            d = deg_raw % 360
            px = cx + (deg_raw - heading) * px_per_deg
            if px < x or px > x + w:
                continue
            if d % 10 == 0:
                Line(points=[px, y, px, y + h * 0.35], width=1)
                if d % 30 == 0:
                    dirs = {0: 'N', 90: 'E', 180: 'S', 270: 'W'}
                    label = dirs.get(d, str(d))
                    tex = self._tex(label, h * 0.32, get_color("hud_heading_label"))
                    self._draw_tex(tex, px - tex.width / 2, y + h * 0.38)
                    Color(*get_color("hud_heading_tick"))
            elif d % 5 == 0:
                Line(points=[px, y, px, y + h * 0.18], width=0.8)

        # Center indicator triangle
        Color(*get_color("hud_center_indicator"))
        ts = h * 0.18
        Line(points=[cx - ts, y + h, cx, y + h - ts, cx + ts, y + h],
             width=1.5, close=True)

        # Heading value box
        bw = max(w * 0.07, 46)
        bh = h * 0.48
        Color(*get_color("bg_value_box"))
        Rectangle(pos=(cx - bw / 2, y + 2), size=(bw, bh))
        tex = self._tex(f"{int(heading):03d}\u00b0", bh * 0.6,
                        get_color("hud_display_green"), bold=True)
        self._draw_tex(tex, cx - tex.width / 2,
                       y + 2 + (bh - tex.height) / 2)

    # -----------------------------------------------------------------
    # Speed tape (left)
    # -----------------------------------------------------------------

    def _draw_speed_tape(self, x, y, w, h):
        Color(*get_color("bg_hud"))
        Rectangle(pos=(x, y), size=(w, h))

        spd = self._groundspeed
        cy = y + h / 2
        scale = h / 30.0

        Color(*get_color("hud_tape_tick"))
        for s in range(0, 50):
            py = cy + (s - spd) * scale
            if py < y or py > y + h:
                continue
            if s % 5 == 0:
                Line(points=[x + w * 0.55, py, x + w, py], width=1)
                tex = self._tex(str(s), w * 0.22, get_color("hud_tape_label"))
                self._draw_tex(tex, x + 2, py - tex.height / 2)
                Color(*get_color("hud_tape_tick"))

        # Current value box
        bh = max(h * 0.07, 20)
        Color(*get_color("bg_value_box"))
        Rectangle(pos=(x, cy - bh / 2), size=(w, bh))
        Color(*get_color("hud_value_border"))
        Line(rectangle=(x, cy - bh / 2, w, bh), width=1)
        tex = self._tex(f"{spd:.1f}", bh * 0.6, get_color("hud_display_green"), bold=True)
        self._draw_tex(tex, x + (w - tex.width) / 2, cy - tex.height / 2)

        # Title label
        tex = self._tex("GS m/s", w * 0.18, get_color("hud_tape_title"))
        self._draw_tex(tex, x + (w - tex.width) / 2,
                       y + h - tex.height - 2)

    # -----------------------------------------------------------------
    # Altitude tape (right)
    # -----------------------------------------------------------------

    def _draw_alt_tape(self, x, y, w, h):
        Color(*get_color("bg_hud"))
        Rectangle(pos=(x, y), size=(w, h))

        alt = self._alt_rel
        cy = y + h / 2
        scale = h / 100.0

        Color(*get_color("hud_tape_tick"))
        for a in range(-20, 200, 5):
            py = cy + (a - alt) * scale
            if py < y or py > y + h:
                continue
            if a % 10 == 0:
                Line(points=[x, py, x + w * 0.4, py], width=1)
                tex = self._tex(str(a), w * 0.22, get_color("hud_tape_label"))
                self._draw_tex(tex, x + w * 0.45, py - tex.height / 2)
                Color(*get_color("hud_tape_tick"))
            else:
                Line(points=[x, py, x + w * 0.2, py], width=0.8)

        bh = max(h * 0.07, 20)
        Color(*get_color("bg_value_box"))
        Rectangle(pos=(x, cy - bh / 2), size=(w, bh))
        Color(*get_color("hud_value_border"))
        Line(rectangle=(x, cy - bh / 2, w, bh), width=1)
        tex = self._tex(f"{alt:.1f}", bh * 0.6, get_color("hud_display_green"), bold=True)
        self._draw_tex(tex, x + (w - tex.width) / 2, cy - tex.height / 2)

        tex = self._tex("ALT m", w * 0.18, get_color("hud_tape_title"))
        self._draw_tex(tex, x + (w - tex.width) / 2,
                       y + h - tex.height - 2)

    # -----------------------------------------------------------------
    # Bottom info bar
    # -----------------------------------------------------------------

    def _draw_info_bar(self, x, y, w, h):
        Color(*get_color("bg_hud"))
        Rectangle(pos=(x, y), size=(w, h))

        vz_ms = -self._vz / 100.0  # positive = climbing
        items = [
            f"VS: {vz_ms:+.1f} m/s",
            f"THR: {self._throttle}%",
            f"AS: {self._airspeed:.1f} m/s",
        ]
        seg = w / len(items)
        fs = max(h * 0.42, 10)
        for i, text in enumerate(items):
            tex = self._tex(text, fs, get_color("text_info_bar"))
            self._draw_tex(tex, x + (i + 0.5) * seg - tex.width / 2,
                           y + (h - tex.height) / 2)
