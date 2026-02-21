"""
Theme definitions for CopterSonde GCS.

Each theme is a dict mapping semantic color names to RGBA tuples.

Two themes are provided:
  - "dark": low-brightness theme for indoor use or dim conditions.
  - "high_contrast": light backgrounds with bold, saturated colors
    designed for outdoor use in direct sunlight where a dark UI
    would be unreadable.

Colors use semantic names (e.g. "btn_connect", "status_warn") so
widgets reference intent, not raw color values.  This makes it easy
to add new themes without touching widget code.
"""

THEMES = {
    # ── Dark theme — indoor / dim conditions ─────────────────────────
    "dark": {
        # -- Backgrounds (darkest to lightest) --
        "bg_plot_area":     (0.06, 0.06, 0.08, 1),
        "bg_hud":           (0.08, 0.08, 0.1, 1),
        "bg_status_log":    (0.08, 0.08, 0.1, 1),
        "bg_plot_widget":   (0.1, 0.1, 0.12, 1),
        "bg_root":          (0.12, 0.12, 0.14, 1),
        "bg_navbar":        (0.15, 0.15, 0.18, 1),
        "bg_tile":          (0.18, 0.18, 0.22, 1),
        "bg_input":         (0.2, 0.2, 0.25, 1),
        "bg_spinner":       (0.25, 0.25, 0.3, 1),
        "bg_map":           (0.06, 0.08, 0.1, 1),
        "bg_map_loading":   (0.1, 0.12, 0.14, 1),
        "bg_overlay":       (0, 0, 0, 0.5),
        "bg_value_box":     (0, 0, 0, 0.85),

        # -- Text --
        "text_primary":     (1, 1, 1, 1),
        "text_title":       (0.8, 0.85, 0.9, 1),
        "text_label":       (0.7, 0.7, 0.7, 1),
        "text_settings":    (0.65, 0.65, 0.7, 1),
        "text_tile_label":  (0.55, 0.6, 0.65, 1),
        "text_axis":        (0.5, 0.5, 0.5, 1),
        "text_section":     (0.45, 0.48, 0.52, 1),
        "text_dim":         (0.4, 0.4, 0.4, 1),
        "text_detail":      (0.6, 0.6, 0.6, 1),
        "text_feedback":    (0.5, 0.7, 0.5, 1),
        "text_cmd_feedback": (0.5, 0.6, 0.7, 1),
        "text_status_log":  (0.6, 0.7, 0.65, 1),
        "text_mode_display": (0.6, 0.65, 0.7, 1),
        "text_info_bar":    (0.7, 0.8, 0.9, 1),
        "text_copy_btn":    (0.85, 0.9, 0.95, 1),
        "text_last_update": (0.5, 0.5, 0.5, 1),
        "text_formula":     (0.5, 0.55, 0.6, 1),

        # -- Status --
        "status_healthy":   (0.15, 0.75, 0.3, 1),
        "status_warn":      (0.9, 0.6, 0.1, 1),
        "status_error":     (0.7, 0.2, 0.2, 1),
        "status_conn_err":  (0.9, 0.4, 0.1, 1),
        "tile_default":     (0.18, 0.18, 0.22, 1),
        "tile_green":       (0.12, 0.45, 0.2, 1),
        "tile_yellow":      (0.55, 0.5, 0.1, 1),
        "tile_red":         (0.6, 0.15, 0.15, 1),
        "armed_color":      (0.9, 0.2, 0.2, 1),
        "disarmed_color":   (0.3, 0.8, 0.4, 1),

        # -- Buttons --
        "btn_connect":      (0.2, 0.55, 0.3, 1),
        "btn_disconnect":   (0.6, 0.2, 0.2, 1),
        "btn_action":       (0.25, 0.35, 0.5, 1),
        "btn_danger":       (0.7, 0.3, 0.15, 1),
        "btn_safe":         (0.2, 0.45, 0.25, 1),
        "btn_warning":      (0.55, 0.35, 0.1, 1),
        "btn_clear":        (0.5, 0.25, 0.2, 1),
        "btn_generate":     (0.25, 0.45, 0.55, 1),
        "btn_apply":        (0.2, 0.5, 0.3, 1),
        "btn_reset":        (0.5, 0.25, 0.2, 1),
        "btn_map":          (0.2, 0.3, 0.4, 1),
        "btn_toggle_on":    (0.15, 0.5, 0.2, 1),
        "btn_toggle_off":   (0.6, 0.18, 0.18, 1),

        # -- HUD --
        "hud_sky":          (0.15, 0.35, 0.65, 1),
        "hud_ground":       (0.45, 0.30, 0.15, 1),
        "hud_horizon":      (1, 1, 1, 0.85),
        "hud_pitch_ladder": (1, 1, 1, 0.5),
        "hud_border":       (0.35, 0.35, 0.4, 1),
        "hud_crosshair":    (1, 0.9, 0, 1),
        "hud_display_green": (0, 1, 0, 1),
        "hud_heading_tick": (1, 1, 1, 0.6),
        "hud_heading_label": (1, 1, 1, 0.85),
        "hud_tape_tick":    (1, 1, 1, 0.5),
        "hud_tape_label":   (1, 1, 1, 0.75),
        "hud_tape_title":   (0.5, 0.55, 0.6, 1),
        "hud_center_indicator": (1, 0.9, 0, 1),
        "hud_roll_readout": (1, 1, 1, 0.7),
        "hud_value_border": (0, 1, 0, 1),

        # -- Plots --
        "plot_border":      (0.3, 0.3, 0.35, 1),
        "plot_grid":        (0.18, 0.18, 0.2, 1),
        "plot_title":       (0.7, 0.75, 0.8, 1),
        "plot_x_label":     (0.45, 0.45, 0.45, 1),

        # -- Map --
        "map_track":        (0.3, 0.7, 0.3, 0.7),
        "map_drone":        (0.3, 1, 0.5, 1),
        "map_adsb":         (1, 0.15, 0.1, 0.9),
        "map_adsb_label":   (1, 0.7, 0.2, 1),
        "map_adsb_label_bg": (0, 0, 0, 0.55),
        "map_scale":        (1, 1, 1, 0.9),
        "map_info":         (0.9, 0.95, 1, 1),
    },

    # ── High-contrast theme — outdoor / direct sunlight ─────────────
    "high_contrast": {
        # -- Backgrounds (light for sun readability) --
        "bg_plot_area":     (0.95, 0.95, 0.93, 1),
        "bg_hud":           (0.90, 0.90, 0.88, 1),
        "bg_status_log":    (0.95, 0.95, 0.93, 1),
        "bg_plot_widget":   (0.92, 0.92, 0.90, 1),
        "bg_root":          (0.88, 0.88, 0.86, 1),
        "bg_navbar":        (0.82, 0.82, 0.80, 1),
        "bg_tile":          (0.85, 0.85, 0.83, 1),
        "bg_input":         (1, 1, 1, 1),
        "bg_spinner":       (0.92, 0.92, 0.90, 1),
        "bg_map":           (0.90, 0.90, 0.88, 1),
        "bg_map_loading":   (0.92, 0.92, 0.90, 1),
        "bg_overlay":       (1, 1, 1, 0.75),
        "bg_value_box":     (1, 1, 1, 0.92),

        # -- Text (dark for sun readability) --
        "text_primary":     (0, 0, 0, 1),
        "text_title":       (0.05, 0.05, 0.1, 1),
        "text_label":       (0.1, 0.1, 0.1, 1),
        "text_settings":    (0.12, 0.12, 0.15, 1),
        "text_tile_label":  (0.15, 0.15, 0.2, 1),
        "text_axis":        (0.12, 0.12, 0.12, 1),
        "text_section":     (0.08, 0.08, 0.12, 1),
        "text_dim":         (0.35, 0.35, 0.35, 1),
        "text_detail":      (0.12, 0.12, 0.15, 1),
        "text_feedback":    (0.0, 0.45, 0.0, 1),
        "text_cmd_feedback": (0.05, 0.15, 0.45, 1),
        "text_status_log":  (0.05, 0.15, 0.1, 1),
        "text_mode_display": (0.1, 0.1, 0.15, 1),
        "text_info_bar":    (0.05, 0.1, 0.2, 1),
        "text_copy_btn":    (1, 1, 1, 1),
        "text_last_update": (0.15, 0.15, 0.15, 1),
        "text_formula":     (0.15, 0.18, 0.22, 1),

        # -- Status (bold, saturated for sun) --
        "status_healthy":   (0.0, 0.55, 0.1, 1),
        "status_warn":      (0.8, 0.5, 0.0, 1),
        "status_error":     (0.8, 0.1, 0.1, 1),
        "status_conn_err":  (0.85, 0.3, 0.0, 1),
        "tile_default":     (0.85, 0.85, 0.83, 1),
        "tile_green":       (0.55, 0.82, 0.55, 1),
        "tile_yellow":      (0.9, 0.85, 0.4, 1),
        "tile_red":         (0.9, 0.5, 0.5, 1),
        "armed_color":      (0.85, 0.05, 0.05, 1),
        "disarmed_color":   (0.0, 0.55, 0.1, 1),

        # -- Buttons (bold, saturated) --
        "btn_connect":      (0.1, 0.6, 0.2, 1),
        "btn_disconnect":   (0.75, 0.15, 0.15, 1),
        "btn_action":       (0.15, 0.35, 0.65, 1),
        "btn_danger":       (0.8, 0.2, 0.05, 1),
        "btn_safe":         (0.1, 0.55, 0.2, 1),
        "btn_warning":      (0.7, 0.45, 0.0, 1),
        "btn_clear":        (0.65, 0.2, 0.15, 1),
        "btn_generate":     (0.15, 0.45, 0.65, 1),
        "btn_apply":        (0.1, 0.55, 0.2, 1),
        "btn_reset":        (0.65, 0.2, 0.15, 1),
        "btn_map":          (0.15, 0.35, 0.55, 1),
        "btn_toggle_on":    (0.1, 0.6, 0.15, 1),
        "btn_toggle_off":   (0.75, 0.15, 0.15, 1),

        # -- HUD (high contrast for sun) --
        "hud_sky":          (0.4, 0.6, 0.9, 1),
        "hud_ground":       (0.65, 0.5, 0.25, 1),
        "hud_horizon":      (0, 0, 0, 1),
        "hud_pitch_ladder": (0, 0, 0, 0.7),
        "hud_border":       (0, 0, 0, 1),
        "hud_crosshair":    (1, 0.15, 0, 1),
        "hud_display_green": (0, 0.45, 0, 1),
        "hud_heading_tick": (0, 0, 0, 0.8),
        "hud_heading_label": (0, 0, 0, 1),
        "hud_tape_tick":    (0, 0, 0, 0.7),
        "hud_tape_label":   (0, 0, 0, 0.9),
        "hud_tape_title":   (0.1, 0.12, 0.18, 1),
        "hud_center_indicator": (1, 0.15, 0, 1),
        "hud_roll_readout": (0, 0, 0, 0.85),
        "hud_value_border": (0, 0.45, 0, 1),

        # -- Plots --
        "plot_border":      (0, 0, 0, 1),
        "plot_grid":        (0.78, 0.78, 0.80, 1),
        "plot_title":       (0.05, 0.08, 0.12, 1),
        "plot_x_label":     (0.12, 0.12, 0.15, 1),

        # -- Map --
        "map_track":        (0.0, 0.5, 0.0, 0.9),
        "map_drone":        (0.0, 0.7, 0.2, 1),
        "map_adsb":         (0.9, 0.0, 0.0, 1),
        "map_adsb_label":   (0.8, 0.4, 0.0, 1),
        "map_adsb_label_bg": (1, 1, 1, 0.7),
        "map_scale":        (0, 0, 0, 1),
        "map_info":         (0.0, 0.05, 0.1, 1),
    },
}

# Display names for the settings UI spinner
THEME_NAMES = {"dark": "Dark", "high_contrast": "High Contrast"}

# Module-level state — switched at runtime via set_theme()
_current_theme = "dark"


def set_theme(name):
    """Set the current theme by name."""
    global _current_theme
    if name in THEMES:
        _current_theme = name


def get_theme_name():
    """Return current theme name."""
    return _current_theme


def get_color(name):
    """Return RGBA tuple for semantic color name in current theme."""
    # Magenta fallback makes missing color keys immediately visible
    # during development without crashing the app.
    return THEMES[_current_theme].get(name, (1, 0, 1, 1))  # magenta = missing
