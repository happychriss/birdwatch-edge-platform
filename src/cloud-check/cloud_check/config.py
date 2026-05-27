from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # ── Bucket layout ────────────────────────────────────────────────────────────
    # Two-level nested model. Outer = photo-bucket (sensor exposure regime, fixed
    # by global_mean thresholds — mirrors the ESP photo modes). Inner = scene-bucket
    # (shadow-pattern cluster within an exposure regime, learned by k-means once
    # enough data exists per photo-bucket). Today K_scene=1: one cell per photo-bucket,
    # populated directly from RTC reference frames.
    num_photo_buckets: int = 3        # NORMAL, BRIGHT, LOWLIGHT — keyed by global_mean
    num_scene_buckets: int = 1        # K_scene per photo-bucket (1 = no clustering yet)

    # photo-bucket boundaries (mirror BW_BRIGHT_PHOTO_THRESHOLD / BW_LOWLIGHT_PHOTO_THRESHOLD in C).
    # global_mean >= bright_photo_threshold → BRIGHT
    # global_mean <  lowlight_photo_threshold → LOWLIGHT
    # otherwise → NORMAL
    bright_photo_threshold: int = 160
    lowlight_photo_threshold: int = 80

    # ── Background model ────────────────────────────────────────────────────────
    # Slower α gives a more stable model so a single object frame can't poison it.
    # The RTC-only update gate (PIR frames never update the model) is the primary
    # safeguard; α controls how fast genuine background drift is tracked.
    ema_alpha: float = 0.15
    var_floor: float = 36.0           # minimum per-tile variance (std=6 on 0-255 scale)
    init_var: float = 256.0           # initial per-tile variance (std=16)

    # ── Decision rule ───────────────────────────────────────────────────────────
    # Bias toward "process" — missing a bird is much worse than uploading a cloud frame.
    tile_z_threshold: float = 3.0
    quiet_anomaly_ratio: float = 0.25     # ≤ this fraction of dark-anomalous tiles → suppress
    dark_object_min_delta: float = 20.0   # tile darker than bucket mean by this much → object-like
    dark_object_min_tiles: int = 1
    dark_blob_max_size: int = 5           # largest qualifying dark blob (≤ this → DARK_BLOB → process)

    # ── Chroma gates ────────────────────────────────────────────────────────────
    # ΔC² = ΔU² + ΔV² (squared form avoids sqrt and matches the ESP integer math).
    # chroma_delta_threshold_sq = 8² = 64 is the burst-filter chroma-changed threshold.
    # chroma_dark_obj_gate_sq is the looser gate the background-model DARK_OBJ stage
    # uses: a tile counts as a dark object if (Y is much darker than model) OR
    # (chroma differs from model by more than the gate). Same value as a safe starting
    # point — sweep before locking the production value.
    chroma_delta_threshold: float = 8.0
    chroma_delta_threshold_sq: int = 64
    chroma_dark_obj_gate_sq: int = 64

    # ── Warmup ──────────────────────────────────────────────────────────────────
    warmup_frames_per_bucket: int = 0  # Python simulation: steady-state (model calibrated).
                                       # C firmware uses CC_WARMUP_FRAMES=4 for first-boot NVS bootstrap.

    # ── Tile grid ───────────────────────────────────────────────────────────────
    # 20×15 at VGA. Changing these requires matching firmware constants.
    grid_w: int = 20
    grid_h: int = 15

