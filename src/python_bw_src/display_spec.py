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
            "batched": ("#fdebd0", "#b9770e"),
        },
        "fallback": ("#eee", "#778"),
        "desc": "Final decision — process: uploaded; clouds: suppressed by the burst filter; batched: suppressed on-device from the clock, thumbnail kept for review",
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
            # HP blur experiment stages
            "DARK_BLOB_HP":   "#16a085",
            "QUIET_HP":       "#7fb3d3",
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
            # Clock-based pre-suppression (runs before the camera)
            "PRESUPPRESS":    "#b9770e",
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
            # Clock-based pre-suppression (runs before the camera)
            "PRESUPPRESS":    "#b9770e",
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
    # Tile overlay — debug arrays for blob visualisation.
    # tile_color_mask: 0=none, 1=blue (dark_model tile), 2=red (qualifying dark blob tile).
    # Backfill-computed fields — suppress from card badge row; visible in detail table.
    # ── clock-based pre-suppression (presuppress.c) ──────────────────────────
    "why": {
        "type": "stage_badge",
        "palette": {
            "SCORE":   "#b9770e",   # table score below threshold → suppressed
            "PROCEED": "#27ae60",   # scored above threshold → uploaded
            "RTC":     "#3498db",   # scheduled frame, never suppressed
            "NO_TIME": "#c0392b",   # RTC unreadable → never suppress on a guess
        },
        "fallback": "#aab",
        "desc": "Why this frame was or was not suppressed before the camera ran.",
    },
    "solar_elev": {"type": "format_val", "format": "{:.1f}°",
                   "desc": "Sun elevation above the horizon. Negative = night. "
                           "Seasonally comparable, unlike clock time."},
    "quiet_gap":  {"type": "numeric",
                   "desc": "Seconds since the previous PIR event. Long quiet means a real "
                           "arrival is far more likely (1.2% birds under 30 s, 10.8% beyond 3 h)."},
    "burst_pos":  {"type": "numeric",
                   "desc": "Position in a run of triggers <60 s apart. Position 4+ contained "
                           "no birds at all across 181 frames."},
    "ps_score":   {"type": "numeric",
                   "desc": "Lookup-table score 0-255; below ps_thr the event is suppressed."},
    "ps_thr":     {"type": "numeric",
                   "desc": "Active threshold (NVS ps_thr). Higher = more aggressive suppression."},
    "batched":    {"type": "numeric", "desc": "Stored on-device and flushed later."},
    "batch_dropped": {"type": "numeric",
                      "desc": "Records dropped for capacity since the last flush. Non-zero means "
                              "the store is undersized or WiFi has been failing for a long time."},

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
    "fw_version":  {"type": "plain",
                    "desc": "Git describe string embedded in the running image. Compared against "
                            "/firmware/version by app_elf_sha256 to decide whether to pull an update."},
    "ota_pending": {"type": "badge",
                    "colors": {"True": ("#fdebd0", "#b9770e"), "1": ("#fdebd0", "#b9770e")},
                    "fallback": ("#eee", "#778"),
                    "desc": "Image booted from OTA and is on probation. It is confirmed only after a "
                            "successful upload; otherwise the bootloader reverts it on the next boot."},
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
# Gallery card: a STRICT whitelist, rendered with render_key_badges so unlisted
# meta keys are never auto-appended.  Before this the card showed 22 badges per
# frame — 12 of them camera internals (awb_*, ettr_*, bracket_*, next_wakeup)
# that nobody reads at thumbnail size.  Exception fields are listed too but cost
# nothing on a normal frame, because absent keys render nothing.
DISPLAY_CARD_FIELDS = [
    "result", "stage", "why",        # what happened, and why it was or was not sent
    "source", "battery",             # rtc vs pir, and the one health number
    "fresh_flash", "ota_pending", "batch_dropped",   # only ever appear when true
]

# Detail page ordering.  The detail table renders every meta key regardless;
# this just puts the interesting ones first.
DISPLAY_ORDER = [
    "result", "stage", "why", "source", "burst_trigger", "fresh_flash",
    "battery", "trigger",
    # clock-based pre-suppression inputs — these decide whether a frame is sent
    "solar_elev", "quiet_gap", "burst_pos", "ps_score", "ps_thr",
    "global_mean", "burst_n_changed", "burst_n_dark", "burst_n_chroma",
    "batch_dropped", "ota_pending",
]
