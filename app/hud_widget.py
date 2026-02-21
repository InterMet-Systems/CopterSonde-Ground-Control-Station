"""
Canvas-drawn Flight HUD widget for CopterSonde GCS.

Displays attitude indicator (artificial horizon with roll/pitch),
heading compass strip, speed and altitude tapes, and bottom info bar.
"""

import math
from collections import OrderedDict

from kivy.uix.widget import Widget
from kivy.graphics import (
    Color, Rectangle, Line,
    PushMatrix, PopMatrix, Rotate, Translate,
    StencilPush, StencilPop, StencilUse, StencilUnUse,
)
from kivy.clock import Clock
from kivy.core.text import Label as CoreLabel

from app.theme import get_color

# LRU text texture cache — avoids re-rasterizing CoreLabel every frame.
# CoreLabel.refresh() is expensive; caching by (text, size, color, bold)
# turns repeated draws into cheap texture lookups.
_TEX_CACHE_MAX = 200


class FlightHUD(Widget):
    """Custom canvas-drawn flight HUD."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Flight state values — updated atomically via set_state()
        self._roll = 0.0
        self._pitch = 0.0
        self._heading = 0.0
        self._airspeed = 0.0
        self._groundspeed = 0.0
        self._alt_rel = 0.0
        self._vz = 0.0
        self._throttle = 0
        # Dirty-flag coalescing: multiple set_state() calls within one
        # frame only produce a single redraw, scheduled for next frame.
        self._dirty = True
        self._redraw_scheduled = False
        self._tex_cache = OrderedDict()  # LRU text texture cache
        self.bind(pos=self._mark_dirty, size=self._mark_dirty)

    def _mark_dirty(self, *_args):
        """Flag that the HUD needs redrawing, coalescing multiple calls."""
        self._dirty = True
        if not self._redraw_scheduled:
            self._redraw_scheduled = True
            # Schedule for next frame — avoids redundant redraws this frame
            Clock.schedule_once(self._do_redraw, 0)

    def _do_redraw(self, _dt=None):
        self._redraw_scheduled = False
        if self._dirty:
            self._dirty = False
            self._redraw()

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
        self._mark_dirty()

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

            # Layout: heading strip (top), speed/alt tapes (sides),
            # attitude indicator (center), info bar (bottom)
            hdg_h = max(h * 0.10, 30)     # heading compass strip height
            info_h = max(h * 0.08, 24)    # bottom info bar height
            tape_w = max(w * 0.14, 50)    # speed/altitude tape width
            gap = 3                        # spacing between sections

            # Attitude indicator fills the remaining central area
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
    # Helpers — LRU text texture cache
    # -----------------------------------------------------------------
    # CoreLabel.refresh() rasterizes a glyph bitmap on every call.
    # By caching the resulting texture keyed on (text, size, color, bold),
    # we avoid per-frame rasterization and only pay the cost on first use.

    def _tex(self, text, font_size, color=(1, 1, 1, 1), bold=False):
        key = (str(text), int(font_size), tuple(color), bold)
        tex = self._tex_cache.get(key)
        if tex is not None:
            self._tex_cache.move_to_end(key)  # mark as recently used
            return tex
        # Cache miss — rasterize and store
        lbl = CoreLabel(text=str(text), font_size=max(font_size, 8),
                        color=color, bold=bold)
        lbl.refresh()
        tex = lbl.texture
        self._tex_cache[key] = tex
        if len(self._tex_cache) > _TEX_CACHE_MAX:
            self._tex_cache.popitem(last=False)  # evict oldest entry
        return tex

    def _draw_tex(self, tex, x, y):
        """Blit a pre-rasterized text texture at (x, y)."""
        Color(1, 1, 1, 1)  # white tint so texture colors pass through
        Rectangle(texture=tex, pos=(x, y), size=tex.size)

    # -----------------------------------------------------------------
    # Attitude indicator (artificial horizon)
    # -----------------------------------------------------------------
    # Uses OpenGL stencil buffer to clip the rotated sky/ground to the
    # rectangular AI area, preventing overdraw onto adjacent tapes.
    # The coordinate transform works as follows:
    #   1. Translate origin to AI center
    #   2. Rotate by roll angle around the Z axis
    #   3. Offset sky/ground vertically by pitch (negative = nose up)

    def _draw_attitude(self, x, y, w, h):
        cx, cy = x + w / 2, y + h / 2
        pitch_scale = h / 40.0  # pixels per degree of pitch
        # Invert: positive pitch (nose up) moves horizon down in screen space
        pitch_px = -math.degrees(self._pitch) * pitch_scale
        roll_deg = math.degrees(self._roll)

        # Stencil-based clipping: only pixels inside the AI rectangle pass
        StencilPush()
        Rectangle(pos=(x, y), size=(w, h))
        StencilUse()

        # Apply roll rotation around the center of the AI area
        PushMatrix()
        Translate(cx, cy, 0)
        Rotate(angle=roll_deg, axis=(0, 0, 1))

        # Sky — oversized rectangle above the horizon line
        Color(*get_color("hud_sky"))
        Rectangle(pos=(-w * 1.5, pitch_px), size=(w * 3, h * 3))

        # Ground — oversized rectangle below the horizon line
        Color(*get_color("hud_ground"))
        Rectangle(pos=(-w * 1.5, pitch_px - h * 3), size=(w * 3, h * 3))

        # Horizon line separating sky and ground
        Color(*get_color("hud_horizon"))
        Line(points=[-w * 1.5, pitch_px, w * 1.5, pitch_px], width=1.5)

        # Pitch ladder (every 5 degrees); wider marks at 10-degree intervals
        Color(*get_color("hud_pitch_ladder"))
        for deg in range(-20, 25, 5):
            if deg == 0:
                continue
            py = pitch_px + deg * pitch_scale
            lw = w * 0.12 if deg % 10 == 0 else w * 0.06
            Line(points=[-lw, py, lw, py], width=1)

        PopMatrix()

        # End stencil clipping
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
    # Heading compass strip
    # -----------------------------------------------------------------
    # Renders a horizontal tape scrolling left/right with heading.
    # The window shows ~90 degrees centered on the current heading.

    def _draw_heading(self, x, y, w, h):
        Color(*get_color("bg_hud"))
        Rectangle(pos=(x, y), size=(w, h))

        heading = self._heading
        px_per_deg = w / 90.0  # 90-degree visible arc across full width
        cx = x + w / 2

        # Iterate over heading range visible in the strip
        start = int(heading) - 50
        Color(*get_color("hud_heading_tick"))
        for deg_raw in range(start, start + 101):
            d = deg_raw % 360  # wrap to [0, 360)
            px = cx + (deg_raw - heading) * px_per_deg
            if px < x or px > x + w:
                continue
            if d % 10 == 0:
                Line(points=[px, y, px, y + h * 0.35], width=1)
                # Cardinal/ordinal labels at 30-degree intervals
                if d % 30 == 0:
                    dirs = {0: 'N', 90: 'E', 180: 'S', 270: 'W'}
                    label = dirs.get(d, str(d))
                    tex = self._tex(label, h * 0.32, get_color("hud_heading_label"))
                    self._draw_tex(tex, px - tex.width / 2, y + h * 0.38)
                    Color(*get_color("hud_heading_tick"))
            elif d % 5 == 0:
                Line(points=[px, y, px, y + h * 0.18], width=0.8)

        # Center indicator triangle pointing down at current heading
        Color(*get_color("hud_center_indicator"))
        ts = h * 0.18
        Line(points=[cx - ts, y + h, cx, y + h - ts, cx + ts, y + h],
             width=1.5, close=True)

        # Numeric heading readout box at bottom-center
        bw = max(w * 0.07, 46)
        bh = h * 0.48
        Color(*get_color("bg_value_box"))
        Rectangle(pos=(cx - bw / 2, y + 2), size=(bw, bh))
        tex = self._tex(f"{int(heading):03d}\u00b0", bh * 0.6,
                        get_color("hud_display_green"), bold=True)
        self._draw_tex(tex, cx - tex.width / 2,
                       y + 2 + (bh - tex.height) / 2)

    # -----------------------------------------------------------------
    # Speed tape (left side)
    # -----------------------------------------------------------------
    # Vertical scrolling tape: tick marks slide up/down with speed.
    # The tape maps a 30 m/s range across the full height, centered
    # on the current groundspeed value.

    def _draw_speed_tape(self, x, y, w, h):
        Color(*get_color("bg_hud"))
        Rectangle(pos=(x, y), size=(w, h))

        spd = self._groundspeed
        cy = y + h / 2         # center line = current value position
        scale = h / 30.0       # pixels per m/s

        Color(*get_color("hud_tape_tick"))
        for s in range(0, 50):
            # Map each speed mark to a Y position relative to current speed
            py = cy + (s - spd) * scale
            if py < y or py > y + h:
                continue
            if s % 5 == 0:
                Line(points=[x + w * 0.55, py, x + w, py], width=1)
                tex = self._tex(str(s), w * 0.22, get_color("hud_tape_label"))
                self._draw_tex(tex, x + 2, py - tex.height / 2)
                Color(*get_color("hud_tape_tick"))

        # Current value readout box at tape center
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
    # Altitude tape (right side)
    # -----------------------------------------------------------------
    # Same scrolling-tape pattern as speed, but maps a 100 m range.

    def _draw_alt_tape(self, x, y, w, h):
        Color(*get_color("bg_hud"))
        Rectangle(pos=(x, y), size=(w, h))

        alt = self._alt_rel
        cy = y + h / 2
        scale = h / 100.0  # pixels per meter

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

        # vz from MAVLink is cm/s with down-positive; invert for display
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
