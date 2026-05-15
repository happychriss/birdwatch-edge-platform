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
    # worse than uploading a spurious cloud frame. We default to "non-cloud"
    # and only suppress when the evidence for "this is just lighting" is clear.
    tile_z_threshold: float = 3.0      # per-tile z-score above which the tile is "anomalous"
    quiet_anomaly_ratio: float = 0.05  # ≤ this fraction of tiles anomalous → essentially identical scene → cloud
    dark_object_min_delta: float = 30.0  # tile became this much darker than bucket mean → object-like
    dark_object_min_tiles: int = 1      # minimum number of such dark tiles required to trigger DARK_OBJ
    temporal_dark_delta: float = 15.0   # tile must be this much darker than PREVIOUS frame → genuinely new dark event

    # Early-operation bias.  Longer warmup turned out to be the single biggest win
    # in the sweep — it catches the visually-invisible birds (ratio≈0 frames)
    # that QUIET would otherwise suppress.
    warmup_frames_per_bucket: int = 8  # below this many observations the bucket leans non-cloud

    # Night gate (Stage 0).  When the frame is too dark for reliable anomaly
    # detection, upload unconditionally.  Proxy for "sun is down" that works
    # without a clock or location — handles overcast days and season shifts.
    # Set to 0 to disable.
    night_brightness_threshold: float = 80.0  # frame-wide tile-mean average below this → NIGHT
