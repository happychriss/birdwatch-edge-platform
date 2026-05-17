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
    tile_z_threshold: float = 3.0      # per-tile z-score above which the tile is "anomalous"
    quiet_anomaly_ratio: float = 0.25  # ≤ this fraction of dark-anomalous tiles → suppress
    dark_object_min_delta: float = 35.0  # tile became this much darker than bucket mean → object-like
    dark_object_min_tiles: int = 1      # minimum number of such dark tiles required to trigger DARK_OBJ
    temporal_dark_delta: float = 20.0   # tile must be this much darker than PREVIOUS frame → genuinely new dark event
    scene_drift_min_tiles: int = 4      # SCENE_DRIFT needs this many persistently-dark tiles

    warmup_frames_per_bucket: int = 0  # Python simulation: steady-state (model already calibrated).
                                        # C firmware uses CC_WARMUP_FRAMES=4 for actual first-boot via NVS.

    # Tile grid.  20×15 at VGA maps to the QQVGA (160×120) lightcheck with
    # 8×8-pixel tiles (300 tiles total).  Better spatial resolution for small/distant
    # birds vs the old 16×12 grid.  Changing these requires matching firmware constants.
    grid_w: int = 20   # number of tile columns
    grid_h: int = 15   # number of tile rows

    # Night gate (Stage 0).  When the frame is too dark for reliable anomaly
    # detection, upload unconditionally.  Proxy for "sun is down" that works
    # without a clock or location — handles overcast days and season shifts.
    night_brightness_threshold: float = 70.0  # frame-wide tile-mean average below this → NIGHT

    # INDIRECT_LIGHT and SPOT_CHANGE stages removed:
    # - INDIRECT_LIGHT: redundant with dark-only QUIET ratio (bright changes no longer
    #   inflate the ratio, so the regime it guarded is already handled).
    # - SPOT_CHANGE: parameter sweep showed it added FP without reducing FN; with
    #   smaller 8×8 tiles DARK_OBJ is sensitive enough to catch small objects directly.
    indirect_light_threshold: float = 0.0   # disabled
    spot_change_max_tiles: int = 0           # disabled
    spot_change_tile_delta: float = 15.0
    spot_change_global_stability: float = 10.0
    spot_change_max_noisy_tiles: int = 20
