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
            # Background-model stages
            "NIGHT":          "#1a1a2e",
            "WARMUP":         "#9b59b6",
            "DARK_OBJ":       "#2ecc71",
            "QUIET":          "#3498db",
            "SCENE_DRIFT":    "#e67e22",
            "AMBIGUOUS":      "#f39c12",
            "CAM_ERR":        "#c0392b",
            # Burst-filter stages
            "FIRST":          "#7f8c8d",
            "ISOLATED":       "#95a5a6",
            "BRIGHTNESS_SHIFT": "#1abc9c",
            "FAST_SHIFT":     "#e74c3c",
            "DUPLICATE":      "#4a4a4a",
            "BRIGHT_STABLE":  "#2471a3",
            "DIFFUSE":        "#154360",
            "SAFE":           "#27ae60",
        },
        "fallback": "#aab",
    },
    "burst_trigger": {
        "type": "stage_badge",
        "palette": {
            "FIRST":          "#7f8c8d",
            "ISOLATED":       "#95a5a6",
            "BRIGHTNESS_SHIFT": "#1abc9c",
            "FAST_SHIFT":     "#e74c3c",
            "DUPLICATE":      "#4a4a4a",
            "BRIGHT_STABLE":  "#2471a3",
            "DIFFUSE":        "#154360",
            "SAFE":           "#27ae60",
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
    "photo_bucket": {
        "type": "badge",
        "colors": {
            "BRIGHT":   ("#fff3cd", "#856404"),
            "NORMAL":   ("#d6eaf8", "#1a6fa8"),
            "LOWLIGHT": ("#e8e8e8", "#555555"),
        },
        "fallback": ("#eee", "#778"),
    },
    "source": {
        "type": "badge",
        "colors": {
            "pir": ("#fde8d8", "#c0392b"),
            "rtc": ("#d1f2eb", "#1a7d5c"),
        },
        "fallback": ("#eee", "#778"),
    },
    "trigger": {
        "type": "hide_values",
        "hide_values": ["Boot", "PIR", "Timer", "Camera Start", "Camera Stop"],
    },
    "scene_bucket": {
        "type": "badge",
        "colors": {
            "0": ("#d2e4f7", "#1a5276"),   # dim
            "1": ("#d5eafd", "#154577"),   # mid-light
            "2": ("#fde8d0", "#7d3c00"),   # mid-bright
            "3": ("#fef9cc", "#7d6200"),   # bright/sun
        },
        "label_map": {
            "0": "Bucket 0 · dim",
            "1": "Bucket 1 · mid",
            "2": "Bucket 2 · bright",
            "3": "Bucket 3 · sun",
        },
        "fallback": ("#eee", "#778"),
    },
    # Numeric intermediates — shown as plain numbers, no badge.
    "global_mean":       {"type": "numeric"},
    "ratio":             {"type": "format_val", "format": "{:.3f}"},
    "dark_anomalous":    {"type": "numeric"},
    "dark_tiles":        {"type": "numeric"},
    "new_dark_tiles":    {"type": "numeric"},
    "warmup":            {"type": "numeric"},
    "prev_valid":        {"type": "numeric"},
    "burst_n_changed":   {"type": "numeric"},
    "burst_n_dark":      {"type": "numeric"},
    "burst_n_chroma":    {"type": "numeric"},
    "n_chroma_changed":  {"type": "numeric"},
    "dark_blob_max":     {"type": "numeric"},
    # Simulated marker — shown when meta was computed by backfill_meta.py, not firmware.
    "simulated": {
        "type": "badge_if",
        "match_value": True,
        "label": "SIM",
        "color": "#7d6608",
        "text_color": "#fff",
    },
    # Large arrays — hide from card view, show collapsed in detail view only.
    "tile_means":           {"type": "detail_only"},
    "tile_means_u":         {"type": "detail_only"},
    "tile_means_v":         {"type": "detail_only"},
    "model_tile_means":     {"type": "detail_only"},
    "model_tile_means_u":   {"type": "detail_only"},
    "model_tile_means_v":   {"type": "detail_only"},
    # Backfill-computed fields — suppress from card badge row; visible in detail table.
    "downloaded_at": {"type": "plain"},
    "burst_label":   {"type": "plain"},
    "burst_gm_diff": {"type": "format_val", "format": "{:.1f} DN"},
    # Firmware-flash marker — badge only on the first frame after a new build.
    "fresh_flash": {
        "type": "badge_if",
        "match_value": True,
        "label": "FLASHED",
        "color": "#6c3483",
        "text_color": "#fff",
    },
    "fw_build":    {"type": "detail_only"},
    # Manual annotation labels (set via keyboard in frame_detail view).
    "label": {
        "type": "badge",
        "colors": {
            "bird":    ("#d5f5e3", "#1e8449"),
            "special": ("#fef9e7", "#7d6608"),
            "ignore":  ("#f4f6f7", "#7f8c8d"),
        },
        "fallback": ("#eee", "#778"),
    },
}

# Key display order for the card info panel (unlisted keys are appended after).
DISPLAY_ORDER = [
    "result", "stage", "burst_trigger", "source", "photo_bucket", "photo_mode", "fresh_flash", "simulated",
    "battery", "trigger",
    "scene_bucket", "global_mean", "ratio", "dark_tiles", "new_dark_tiles", "dark_blob_max",
    "n_chroma_changed", "burst_n_changed", "burst_n_dark", "burst_n_chroma",
]
