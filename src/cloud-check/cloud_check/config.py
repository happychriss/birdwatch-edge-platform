from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # ── Bucket layout ────────────────────────────────────────────────────────────
    # Two-level nested model. num_photo_buckets=1 (single global model) is the
    # default: affine illumination normalization handles continuous brightness
    # variation per-frame, making fixed exposure buckets redundant (1-bucket
    # affine floor 9.65 < 6-bucket absolute 13.96 in experiments).
    num_photo_buckets: int = 1         # 1 = single global model
    num_scene_buckets: int = 1         # K_scene per photo-bucket (1 = no clustering yet)

    # photo-bucket boundaries — only used when num_photo_buckets > 1.
    bright_photo_threshold: int = 160
    lowlight_photo_threshold: int = 80

    # ── Background model ─────────────────────────────────────────────────────────
    ema_alpha: float = 0.15
    var_floor: float = 36.0            # minimum per-tile variance (std=6 on 0-255 scale)
    init_var: float = 256.0            # initial per-tile variance (std=16)

    # ── Affine illumination normalization ────────────────────────────────────────
    # Per frame, fit T[i] ≈ a·M[i] + b (trimmed least-squares over background tiles).
    # (a,b) is the continuous illumination state; residual delta = (a·M+b) − T is
    # illumination-invariant. Absorbs both multiplicative (sun/cloud scaling) and
    # additive (haze/dusk glow) components. A local object (bird) cannot be explained
    # by a single global affine and survives in the residual. Validated vs gain-only:
    # affine bird margin [22.2, 19.6] DN; gain-only crushes to [14.1, 11.1] DN.
    use_affine_normalization: bool = True
    affine_model_floor: float = 20.0    # ignore near-black model tiles in the fit
    affine_trim_fraction: float = 0.10  # drop worst this fraction before refitting
    affine_min_valid_tiles: int = 10    # need at least this many above-floor tiles

    # Chroma shifts globally too (warm at sunset). Subtract the median ΔU/ΔV across
    # the frame before the chroma gate so a uniform colour shift is absorbed.
    use_chroma_normalization: bool = True

    # ── Decision rule ────────────────────────────────────────────────────────────
    tile_z_threshold: float = 3.0
    quiet_anomaly_ratio: float = 0.25
    dark_object_min_delta: float = 20.0
    dark_object_min_tiles: int = 1
    dark_blob_max_size: int = 5

    # ── Texture signal ───────────────────────────────────────────────────────────
    # Per-tile Y stddev (decoded from JPG). Bird plumage → high std_y. Smooth
    # sky/cloud dimming → low std_y. Second signal independent of illumination.
    # Only active when tile_std_y is passed into classify().
    texture_min_std_y: float = 12.0    # tile Y stddev above this → texturally interesting
    texture_dark_delta: float = 10.0   # looser dark threshold for texture+dark compound

    # ── Chroma gates ─────────────────────────────────────────────────────────────
    chroma_delta_threshold: float = 8.0
    chroma_delta_threshold_sq: int = 64
    chroma_dark_obj_gate_sq: int = 64

    # ── Warmup ───────────────────────────────────────────────────────────────────
    warmup_frames_per_bucket: int = 0

    # ── Tile grid ────────────────────────────────────────────────────────────────
    grid_w: int = 20
    grid_h: int = 15
