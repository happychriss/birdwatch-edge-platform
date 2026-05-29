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

Optional "desc" key on any entry: one-line explanation shown as a grey third column
in the frame_detail parameter table.
"""

DISPLAY_SPEC: dict = {
    "result": {
        "type": "badge",
        "colors": {
            "clouds":  ("#d6eaf8", "#1a6fa8"),
            "process": ("#d5f5e3", "#1e8449"),
        },
        "fallback": ("#eee", "#778"),
        "desc": "Final decision — process: bird/event candidate uploaded; clouds: suppressed as false trigger",
    },
    "stage": {
        "type": "stage_badge",
        "palette": {
            # Background-model stages (Layer-2)
            "WARMUP":         "#9b59b6",
            "DARK_BLOB":      "#2ecc71",
            "QUIET":          "#3498db",
            "AMBIGUOUS":      "#f39c12",
            "CAM_ERR":        "#c0392b",
            # Legacy stages (historical data only)
            "NIGHT":          "#1a1a2e",
            "DARK_OBJ":       "#27ae60",
            "SCENE_DRIFT":    "#e67e22",
            # Burst-filter stages (Layer-1)
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
        "desc": "Pipeline stage: DARK_BLOB=compact bird-sized dark cluster, QUIET=scene calm, WARMUP=model warming up, DUPLICATE=identical re-fire, BRIGHTNESS_SHIFT=whole-scene shift, DIFFUSE=cloud shadow",
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
            "NIGHT":          "#1a1a2e",
            "DIFFUSE":        "#154360",
            "SAFE":           "#27ae60",
        },
        "fallback": "#aab",
        "desc": "Burst pre-filter (Layer-1) result comparing frame to previous. NIGHT fires here — raw sensor property, no model needed.",
    },
    "source": {
        "type": "badge",
        "colors": {
            "pir": ("#fde8d8", "#c0392b"),
            "rtc": ("#d1f2eb", "#1a7d5c"),
        },
        "fallback": ("#eee", "#778"),
        "desc": "Wakeup source — pir: motion sensor; rtc: scheduled 15-min reference cycle (only RTC frames update the background model)",
    },
    "photo_bucket": {
        "type": "badge",
        "colors": {
            "BRIGHT":   ("#fff3cd", "#856404"),
            "NORMAL":   ("#d6eaf8", "#1a6fa8"),
            "LOWLIGHT": ("#e8e8e8", "#555555"),
        },
        "fallback": ("#eee", "#778"),
        "desc": "Exposure regime from metering shot — BRIGHT: gm ≥ 160, NORMAL: 80–159, LOWLIGHT: < 80. Selects the camera AEC profile and indexes the background model.",
    },
    "battery": {
        "type": "format_val",
        "format": "{:.2f} V",
        "warn_below": 3.6,
        "desc": "Battery voltage at capture time. Below 3.6 V device may shut down mid-cycle.",
    },
    "global_mean": {
        "type": "numeric",
        "desc": "Mean Y (luma) across all 300 tiles, 0–255. Used to pick photo_bucket (BRIGHT/NORMAL/LOWLIGHT) and to detect NIGHT (< 70).",
    },
    "ratio": {
        "type": "format_val",
        "format": "{:.3f}",
        "desc": "dark_tiles / 300. ≤ 0.25 → QUIET (suppress). Measures fraction of tiles with confirmed dark anomalies.",
    },
    "dark_tiles": {
        "type": "numeric",
        "desc": "Tiles ≥ 20 DN darker than model mean (Y drop) OR chroma-shifted vs model. ≥ 1 required for DARK_BLOB. Shown in blue in tile overlay.",
    },
    "dark_blob_max": {
        "type": "numeric",
        "desc": "Largest 8-connected cluster of dark_tiles. 1–5 → DARK_BLOB (bird-sized). > 5 → AMBIGUOUS (too large to be a bird). Shown in red in tile overlay.",
    },
    "texture_blob_max": {
        "type": "numeric",
        "desc": "Largest compact blob on texture+dark mask (std_y > 12 AND ΔY > 10 DN loosely). 1–5 → texture trigger for DARK_BLOB (second detection channel for structured plumage). 0 if texture signal inactive.",
    },
    "n_chroma_changed": {
        "type": "numeric",
        "desc": "Tiles where ΔU² + ΔV² > 64 vs background model mean (chroma shift ≥ 8). Real objects (pigeons, people) shift chroma; cloud shadows do not.",
    },
    "burst_n_changed": {
        "type": "numeric",
        "desc": "Tiles where |Y_cur − Y_prev| > 12 DN (any direction vs previous frame). Zero → DUPLICATE suppression candidate.",
    },
    "burst_n_dark": {
        "type": "numeric",
        "desc": "Tiles that got ≥ 12 DN darker vs previous frame. ≥ 60 → DIFFUSE (cloud shadow sweeping full scene).",
    },
    "burst_n_chroma": {
        "type": "numeric",
        "desc": "Tiles where ΔU² + ΔV² > 64 vs previous frame. Must also be zero (alongside burst_n_changed) for DUPLICATE to fire.",
    },
    "burst_gm_diff": {
        "type": "format_val",
        "format": "{:.1f} DN",
        "desc": "Global mean Y change vs previous frame. > 12 DN → BRIGHTNESS_SHIFT (whole-scene lighting event; passes through regardless of tile pattern).",
    },
    "dark_anomalous":    {"type": "numeric"},
    "warmup":            {"type": "numeric"},
    "prev_valid":        {"type": "numeric"},
    # Simulated marker — shown when meta was computed by backfill_meta.py, not firmware.
    "simulated": {
        "type": "badge_if",
        "match_value": True,
        "label": "SIM",
        "color": "#7d6608",
        "text_color": "#fff",
        "desc": "Meta was re-derived from the JPEG by backfill_meta.py (Python simulation), not emitted by the ESP firmware.",
    },
    # Large arrays — hide from card view, show collapsed in detail view only.
    "tile_means":           {"type": "detail_only", "desc": "300 Y (luma) tile means, 20×15 grid, uint8 (0–255). Drives the tile overlay Δm/Δp display."},
    "tile_means_u":         {"type": "detail_only", "desc": "300 U (Cb) tile means, BT.601 full-range, centred at 128."},
    "tile_means_v":         {"type": "detail_only", "desc": "300 V (Cr) tile means, BT.601 full-range, centred at 128."},
    "model_tile_means":     {"type": "detail_only", "desc": "Background model Y means snapshot before this frame's update. Used for Δm in the tile overlay."},
    "model_tile_means_u":   {"type": "detail_only"},
    "model_tile_means_v":   {"type": "detail_only"},
    # Tile overlay — debug arrays for blob visualisation.
    # tile_color_mask: 0=none, 1=blue (dark_model tile), 2=red (qualifying dark blob tile).
    "tile_color_mask":      {"type": "detail_only", "desc": "300 tile classification values: 0=background, 1=dark_model tile (blue, Δluma ≥ 20 or chroma-shifted), 2=dark_blob tile (red, in compact 1–5 tile cluster)."},
    "tile_delta_luma":      {"type": "detail_only", "desc": "300 per-tile Δluma = model_y − tile_y (positive = tile darker than model). Threshold for blue: ≥ 20 DN."},
    "tile_delta_chroma":    {"type": "detail_only", "desc": "300 per-tile Δchroma = √(ΔU² + ΔV²) vs background model mean. Threshold for chroma contribution: √64 ≈ 8 DN."},
    # Backfill-computed fields — suppress from card badge row; visible in detail table.
    "downloaded_at": {"type": "plain"},
    "burst_label":   {"type": "plain", "desc": "Burst filter decision label (mirrors burst_trigger stage name)."},
    # Firmware-flash marker — badge only on the first frame after a new build.
    "fresh_flash": {
        "type": "badge_if",
        "match_value": True,
        "label": "FLASHED",
        "color": "#6c3483",
        "text_color": "#fff",
        "desc": "First frame captured after a new firmware flash. Background model was reset.",
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
        "desc": "Manual training label — bird: confirmed bird, special: noteworthy non-bird, ignore: delete/false positive.",
    },
    "trigger": {
        "type": "hide_values",
        "hide_values": ["Boot", "PIR", "Timer", "Camera Start", "Camera Stop"],
    },
    # Raw ESP payload stored on every /frame upload for SIM vs ESP comparison.
    # Rendered as a collapsible block; the frame_detail toggle uses it directly.
    "esp_meta": {
        "type": "detail_only",
        "desc": "Raw ESP payload snapshot (before server-side reprocessing). View with the ESP toggle button above.",
    },
}

# Key display order for the card info panel (unlisted keys are appended after).
DISPLAY_ORDER = [
    "result", "stage", "burst_trigger", "source", "photo_bucket", "fresh_flash", "simulated",
    "battery", "trigger",
    "global_mean", "ratio", "dark_tiles", "dark_blob_max", "texture_blob_max",
    "n_chroma_changed", "burst_n_changed", "burst_n_dark", "burst_n_chroma",
]
