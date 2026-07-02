"""Configuration in two layers, scaled to the monitor.

user_settings.toml (repo root) holds what a player actually chooses: lane keys, exit
key, note skin, lane mode. Everything the engine was TUNED with (timings, pixel
geometry, detector thresholds, diagnostics) ships as the in-code defaults below, so a
fresh clone runs the exact validated build with no extra files. A local
internal/internal_settings.toml (kept out of the public tree) overrides those defaults
for tuning experiments, and any advanced key can also be pinned from
user_settings.toml, which always wins.

Every pixel and pixel-velocity value was measured on a 1440p highway; scale_config()
maps them to other monitor heights so the geometry ratios the engine was validated
with survive the resolution change. Times and fractions are resolution-free and pass
through untouched.
"""
import math
import os

try:
    import tomllib  # stdlib on Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # identical parser, backported (this project pins 3.9)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INTERNAL_DEFAULTS = {
    "min_confidence": 0.62,
    "min_tile_pixels_top_offset_scaled": 125,
    "tile_filename_suffix": "",
    "debug_positions": "true",
    "white_tile_min_confidence": 0.62,
    "battlestage_tile_min_confidence": 0.62,
    "diamond_tile_min_confidence": 0.95,
    "strip_top_fraction": 0.77,
    "strip_bottom_fraction": 0.84,
    "strip_brightness": 150,
    "strip_min_fraction": 0.15,
    "strip_sustain_fraction": 0.08,
    "strip_inset": 0.15,
    "use_lookahead": "true",
    "upper_strip_top_fraction": 0.25,
    "upper_strip_bottom_fraction": 0.33,
    "lookahead_lead_ms": 120,
    "use_queue": "true",
    "scroll_px_per_s": 840,
    "note_latency_ms": 40,
    "note_match_tol_ms": 60,
    "note_regap_ms": 80,
    "note_detect_bottom_fraction": 0.81,
    "note_min_row_fill": 0.55,
    "note_min_run": 3,
    "use_gem_track": "true",
    "gem_match_px": 45,
    "gem_restrum_gap_ms": 30,
    "gem_min_press_interval_ms": 80,
    "gem_flash_suppress_ms": 120,
    "gem_tap_hold_ms": 400,
    "gem_flash_settle_ms": 150,
    "gem_flash_zone_px": 25,
    "gem_flash_zone_ms": 250,
    "gem_trail_margin_px": 30,
    "gem_trail_certain_px": 88,
    "gem_trail_early_ms": 80,
    "gem_lift_window_ms": 130,
    "gem_lift_fwd_ms": 70,
    "gem_lift_beam_slack_px": 45,
    "use_arrow_lifts": "false",
    "gem_promote_v_min": 400,
    "gem_promote_v_max": 1300,
    "gem_provisional_ttl_ms": 80,
    "gem_promote_min_span_ms": 30,
    "gem_promote_min_dy_px": 25,
    "gem_promote_lead_px": 45,
    "gem_beam_min_height": 80,
    "gem_beam_sustain_height": 45,
    "gem_max_hold_ms": 4000,
    "beam_min_width": 10,
    "beam_max_width": 50,
    "beam_anchor_px": 8,
    "gem_min_height": 4,
    "gem_max_height": 65,
    "gem_merge_gap": 14,
    "gem_merge_center_px": 62,
    "gem_hollow_fill": 0.12,
    "gem_min_mean_brightness": 195,
    "hold_probe_ms": 45,
    "hold_release_grace_ms": 40,
    "diagnostic_capture": "false",
    "diagnostic_fps": 30,
    "diagnostic_max_frames": 14000,
    "log_detection": "false",
    "fk_capture_max": 250,
    "capture_fps": 200
}


PX_KEYS = frozenset({
    "gem_match_px", "gem_flash_zone_px", "gem_trail_margin_px", "gem_trail_certain_px",
    "gem_lift_beam_slack_px", "gem_promote_min_dy_px", "gem_promote_lead_px",
    "gem_beam_min_height", "gem_beam_sustain_height", "gem_min_height", "gem_max_height",
    "gem_merge_gap", "gem_merge_center_px", "beam_min_width", "beam_max_width",
    "beam_anchor_px", "note_min_run",
})

VELOCITY_KEYS = frozenset({"scroll_px_per_s", "gem_promote_v_min", "gem_promote_v_max"})

# explicit so an unclassified new key fails the coverage test; min_tile_pixels_top_offset_scaled
# is 1080-anchored and already scaled in bot.py -- moving it to PX_KEYS double-scales the hit zone
UNSCALED_KEYS = frozenset({
    "min_tile_pixels_top_offset_scaled",
    "lookahead_lead_ms", "note_latency_ms", "note_match_tol_ms", "note_regap_ms",
    "gem_restrum_gap_ms", "gem_min_press_interval_ms", "gem_flash_suppress_ms",
    "gem_tap_hold_ms", "gem_flash_settle_ms", "gem_flash_zone_ms", "gem_trail_early_ms",
    "gem_lift_window_ms", "gem_lift_fwd_ms", "gem_provisional_ttl_ms",
    "gem_promote_min_span_ms", "gem_max_hold_ms", "hold_probe_ms", "hold_release_grace_ms",
    "strip_top_fraction", "strip_bottom_fraction", "strip_min_fraction",
    "strip_sustain_fraction", "strip_inset", "upper_strip_top_fraction",
    "upper_strip_bottom_fraction", "note_detect_bottom_fraction", "note_min_row_fill",
    "gem_hollow_fill",
    "strip_brightness", "gem_min_mean_brightness", "min_confidence",
    "white_tile_min_confidence", "battlestage_tile_min_confidence",
    "diamond_tile_min_confidence",
    "tile_filename_suffix", "debug_positions", "use_lookahead", "use_queue",
    "use_gem_track", "use_arrow_lifts", "diagnostic_capture", "diagnostic_fps",
    "diagnostic_max_frames", "log_detection", "fk_capture_max", "capture_fps",
})


def _scale_px(value, scale):
    return max(1, int(value * scale + 0.5))


def scale_config(config, monitor_height, reference_height=1440):
    """New dict with pixel/velocity keys rescaled from the 1440p tuning anchor."""
    if monitor_height == reference_height:
        return dict(config)
    scale = monitor_height / reference_height
    scaled = dict(config)
    for key in PX_KEYS | VELOCITY_KEYS:
        if key in scaled:
            scaled[key] = _scale_px(scaled[key], scale)
    return scaled


def _load_toml(path):
    # The engine's flag checks compare against "true"/"false" strings (heritage from
    # the pre-TOML config), while TOML users write natural booleans -- normalize at
    # the boundary so both spellings behave identically.
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return {k: ("true" if v is True else "false" if v is False else v)
            for k, v in data.items()}


def load_config(user_path=None, internal_path=None):
    if user_path is None:
        user_path = os.path.join(_ROOT, "user_settings.toml")
    if internal_path is None:
        internal_path = os.path.join(_ROOT, "internal", "internal_settings.toml")
    config = dict(INTERNAL_DEFAULTS)
    if os.path.isfile(internal_path):
        config.update(_load_toml(internal_path))
    config.update(_load_toml(user_path))
    return config


# Values in on-screen pixels: distances, sizes, thresholds along the highway.
PX_KEYS = frozenset({
    "gem_match_px", "gem_flash_zone_px", "gem_trail_margin_px", "gem_trail_certain_px",
    "gem_lift_beam_slack_px", "gem_promote_min_dy_px", "gem_promote_lead_px",
    "gem_beam_min_height", "gem_beam_sustain_height", "gem_min_height", "gem_max_height",
    "gem_merge_gap", "gem_merge_center_px", "beam_min_width", "beam_max_width",
    "beam_anchor_px", "note_min_run",
})

# Values in pixels PER SECOND: the scroll model and the promotion velocity window.
VELOCITY_KEYS = frozenset({"scroll_px_per_s", "gem_promote_v_min", "gem_promote_v_max"})

# Everything else is resolution-free (times, fractions, brightness, flags, diagnostics).
# min_tile_pixels_top_offset_scaled stays here: bot.py already rescales it itself
# against its historical 1080p anchor, so scaling it again would double-apply.
UNSCALED_KEYS = frozenset(INTERNAL_DEFAULTS) - PX_KEYS - VELOCITY_KEYS


def scale_config(config, monitor_height):
    """Return a copy of `config` with px/velocity values mapped from 1440p to the monitor.

    Rounding is HALF-UP, not Python's banker's round(): the validated 1080p table has
    22.5 -> 23 (gem_trail_margin_px) and round() would flip it to 22, silently changing
    live geometry from what runs 15-18 proved. A scaled px value never drops below 1 —
    zero would DISABLE the rule it parameterizes rather than scale it.
    """
    factor = monitor_height / 1440.0
    scaled = dict(config)
    for key in PX_KEYS | VELOCITY_KEYS:
        if key in scaled:
            scaled[key] = max(1, int(math.floor(scaled[key] * factor + 0.5)))
    return scaled
