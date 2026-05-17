"""
display_spec.py — the single place that controls how meta fields are rendered.

Each key maps to a render rule.  Fields NOT listed render as plain "key: value" rows
automatically — no code change needed to merely show a new ESP telemetry value.

To style a new field: add one entry here.  To add a new ESP value: just emit it via
bw_tele_* on the firmware — it appears in the UI immediately as a plain row.

Rule types
----------
badge       : coloured pill badge; "colors" maps value → (bg, fg); unlisted values get
              the fallback colour.
stage_badge : coloured pill using "palette" (value → hex colour); white text.
format_val  : format string applied to the numeric value (Python str.format style).
badge_if    : only render as a badge when the value equals "match_value".
hide_values : list of values to suppress entirely (don't render a row at all).
numeric     : display as plain number (no special styling).
"""

DISPLAY_SPEC: dict = {
    "result": {
        "type": "badge",
        "colors": {
            "clouds":  ("#d6eaf8", "#1a6fa8"),
            "process": ("#d5f5e3", "#1e8449"),
        },
        "fallback": ("#eee", "#778"),
    },
    "stage": {
        "type": "stage_badge",
        "palette": {
            "NIGHT":          "#1a1a2e",
            "WARMUP":         "#9b59b6",
            "DARK_OBJ":       "#2ecc71",
            "INDIRECT_LIGHT": "#e74c3c",
            "SPOT_CHANGE":    "#ff6b35",
            "QUIET":          "#3498db",
            "SCENE_DRIFT":    "#e67e22",
            "AMBIGUOUS":      "#f39c12",
            "CAM_ERR":        "#c0392b",
        },
        "fallback": "#aab",
    },
    "battery": {
        "type": "format_val",
        "format": "{:.2f} V",
        "warn_below": 3.6,   # adds red tint when value < threshold
    },
    "photo_mode": {
        "type": "badge",
        "colors": {
            "BRIGHT":   ("#fff3cd", "#856404"),
            "NORMAL":   ("#d5f5e3", "#1e8449"),
            "LOWLIGHT": ("#fdebd0", "#d35400"),
        },
        "fallback": ("#eee", "#778"),
    },
    "trigger": {
        "type": "hide_values",
        "hide_values": ["Boot", "PIR", "Timer", "Camera Start", "Camera Stop"],
    },
    # Numeric intermediates — shown as plain numbers, no badge.
    "global_mean":   {"type": "numeric"},
    "ratio":         {"type": "format_val", "format": "{:.3f}"},
    "dark_anomalous": {"type": "numeric"},
    "dark_tiles":    {"type": "numeric"},
    "new_dark_tiles": {"type": "numeric"},
    "warmup":        {"type": "numeric"},
    "prev_valid":    {"type": "numeric"},
    # tile_means is a large array — hide from card view, show in detail view only.
    "tile_means":    {"type": "detail_only"},
    # Firmware-flash marker — badge only on the first frame after a new build.
    "fresh_flash": {
        "type": "badge_if",
        "match_value": True,
        "label": "FLASHED",
        "color": "#6c3483",
        "text_color": "#fff",
    },
    "fw_build":    {"type": "detail_only"},
}

# Key display order for the card info panel (unlisted keys are appended after).
DISPLAY_ORDER = [
    "result", "stage", "photo_mode", "fresh_flash",
    "battery", "trigger",
    "global_mean", "ratio", "dark_tiles", "new_dark_tiles",
]
