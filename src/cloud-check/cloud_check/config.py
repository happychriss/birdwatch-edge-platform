from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # Time bucketing. Coarse on purpose: fewer buckets warm up faster, and the
    # shadow pattern in this scene is dominated by 4 sun positions (low east,
    # high south-east, high south-west, low west / shaded).
    num_time_buckets: int = 4         # day split into N equal periods between sunrise..sunset window
    day_start_hour: int = 6
    day_end_hour: int = 22

    # Background model.  Values picked by the grid sweep in scripts/sweep.py:
    # slower α gives a more stable model so a single object frame can't poison it.
    ema_alpha: float = 0.15            # how fast the per-tile mean tracks accepted-cloud frames
    var_floor: float = 36.0            # minimum per-tile variance (on 0-255 scale, std=6)
    init_var: float = 256.0            # initial per-tile variance (std=16) — wide but not infinite

    # Decision rule. Built around the asymmetry: missing a bird/person is much
    # worse than uploading a spurious cloud frame. We default to "process"
    # and only suppress when the evidence for "this is just lighting" is clear.
    tile_z_threshold: float = 2.5      # per-tile z-score above which the tile is "anomalous"
    quiet_anomaly_ratio: float = 0.20  # ≤ this fraction of tiles anomalous → essentially identical scene → cloud
    dark_object_min_delta: float = 35.0  # tile became this much darker than bucket mean → object-like
    dark_object_min_tiles: int = 1      # minimum number of such dark tiles required to trigger DARK_OBJ
    temporal_dark_delta: float = 20.0   # tile must be this much darker than PREVIOUS frame → genuinely new dark event
    scene_drift_min_tiles: int = 4      # SCENE_DRIFT needs this many persistently-dark tiles (bigger than DARK_OBJ);
                                        # fewer tiles → falls through to AMBIGUOUS (still uploads)

    # Early-operation bias.  Longer warmup turned out to be the single biggest win
    # in the sweep — it catches the visually-invisible birds (ratio≈0 frames)
    # that QUIET would otherwise suppress.
    warmup_frames_per_bucket: int = 0  # Python simulation: steady-state (model already calibrated).
                                        # C firmware uses CC_WARMUP_FRAMES=8 for actual first-boot via NVS.

    # Tile grid.  16×12 at VGA maps to the QQVGA (160×120) lightcheck with
    # 10×10-pixel tiles.  32×24 at VGA maps to QVGA (320×240) with the same
    # 10×10-pixel tile size — 4× more tiles, better spatial resolution for
    # small objects.  Changing these requires matching firmware constants.
    grid_w: int = 16   # number of tile columns
    grid_h: int = 12   # number of tile rows

    # Night gate (Stage 0).  When the frame is too dark for reliable anomaly
    # detection, upload unconditionally.  Proxy for "sun is down" that works
    # without a clock or location — handles overcast days and season shifts.
    # Set to 0 to disable.
    night_brightness_threshold: float = 70.0  # frame-wide tile-mean average below this → NIGHT

    # Indirect-light gate (Stage 2b, after DARK_OBJ).  Low-to-moderate brightness
    # combined with high spatial contrast (sun from the side, hard shadows) creates
    # a regime where the background model z-scores are unreliable: the model has
    # accumulated high variance from sun/cloud cycling, so even a 100 DN object
    # delta yields z < 2.5.  In this zone we cannot distinguish a cloud shadow from
    # a small dark object, so we admit the limitation and upload unconditionally.
    # Sits above night_brightness_threshold; set to 0 to disable.
    indirect_light_threshold: float = 95.0  # global_mean below this (but above night) → INDIRECT_LIGHT

    # Spot-change gate (Stage 4, before QUIET).  Detects small localised dark objects
    # that DARK_OBJ misses because their per-tile z-score vs the background model is
    # below the threshold (model not yet calibrated, object too low-contrast, etc.).
    # Fires when the frame is globally stable compared to the previous capture (sun
    # angle / cloud cover unchanged) but exactly 1..spot_change_max_tiles tiles
    # darkened by ≥ spot_change_tile_delta DN.  Also requires that the overall
    # tile-level churn vs the previous frame is low: if many tiles changed a little
    # (shadow redistribution pattern) that is cloud, not bird.
    # Set spot_change_max_tiles to 0 to disable the gate entirely.
    spot_change_max_tiles: int = 2         # max tiles darkened vs prev for SPOT_CHANGE to fire
    spot_change_tile_delta: float = 15.0   # DN — tile must darken this much vs prev frame
    spot_change_global_stability: float = 10.0  # max allowed global_mean shift vs prev frame
    spot_change_max_noisy_tiles: int = 20  # max tiles with |any| change ≥10 DN (excludes shadow churn)
