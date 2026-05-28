from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import label as cc_label

from .background import BackgroundModel, photo_bucket_idx
from .config import Config


@dataclass
class ClassifierResult:
    label: str                    # "clouds" or "process"
    trigger: str                  # WARMUP | DARK_BLOB | QUIET | AMBIGUOUS
    anomaly_mask: np.ndarray      # (GRID_H, GRID_W) bool — tiles with z > threshold AND darker than model
    blob_max_size: int            # largest connected anomalous region (z-mask)
    dark_blob_max: int            # largest connected blob on absolute-delta dark mask
    anomaly_ratio: float          # dark-only anomaly ratio (tiles darker than model / total)
    compactness: float            # blob_max / total_anomalies (0..1, 1 = single solid blob)
    reason: str                   # human-readable explanation
    warmup: bool                  # cell is still in warmup
    dark_tile_mask: np.ndarray    # (GRID_H, GRID_W) bool — tiles qualifying as dark vs model (blue in overlay)
    dark_blob_mask: np.ndarray    # (GRID_H, GRID_W) bool — tiles in qualifying compact blobs (red in overlay)
    tile_delta_luma: np.ndarray   # (GRID_H, GRID_W) float — model_y − tile_y (positive = darker than model)
    dark_tiles: int = 0           # tiles ≥ dark_object_min_delta darker than model (no z-gate)
    photo_bucket: str = "NORMAL"  # outer bucket name (NORMAL | BRIGHT | LOWLIGHT)
    scene_bucket: int = 0         # inner bucket index (0..K_scene-1; today always 0)
    n_chroma_changed: int = 0     # tiles where chroma differs from model beyond gate (0 if no chroma)
    chroma_delta_max: float = 0.0 # max ΔU²+ΔV² across all tiles (0 if no chroma) — diagnostic
    tile_delta_chroma: np.ndarray | None = None  # (GRID_H, GRID_W) float — √(ΔU²+ΔV²) vs model
    illum_a: float = 1.0          # fitted affine slope (a in a·M+b; 1.0 = no normalization)
    illum_b: float = 0.0          # fitted affine offset (b in a·M+b; 0.0 = no normalization)
    texture_blob_max: int = 0     # largest compact blob on texture+dark mask (0 if no std_y)


def classify(
    tile_mean: np.ndarray,
    model: BackgroundModel,
    cfg: Config | None = None,
    tile_mean_u: np.ndarray | None = None,
    tile_mean_v: np.ndarray | None = None,
    tile_std_y: np.ndarray | None = None,
) -> ClassifierResult:
    """Background-model classifier (Layer-2). Called only on frames that passed Layer-1.

    NIGHT is handled by the burst pre-filter (Layer-1); this function never sees dark frames.
    Stages in priority order:
        WARMUP    — bucket cell still bootstrapping → process
        QUIET     — scene matches model (ratio ≤ threshold) → suppress
        DARK_BLOB — compact dark cluster (1..dark_blob_max_size tiles) → process (bird)
        AMBIGUOUS — default → process (safety bias)

    Illumination: per-frame robust affine fit T ≈ a·M+b over background tiles
    (worst affine_trim_fraction dropped before refit). Residual = (a·M+b)−T is
    illumination-invariant; uniform sun/cloud changes cancel out.

    Chroma (U, V): when provided, a tile qualifies as dark if Y drop ≥ threshold OR
    (chroma shift vs model is large AND tile is darker on Y). QUIET ratio is Y-only.

    Texture (std_y): when provided, tiles with Y stddev > texture_min_std_y AND at
    least loosely dark form a second compact-blob signal (OR with dark_blob_condition).
    """

    cfg = cfg or model.cfg

    global_mean = int(tile_mean.mean())   # truncate, matches ESP integer division
    pb_name = model.photo_bucket_for(global_mean)
    pb = photo_bucket_idx(pb_name)
    sb = model.scene_bucket_for(pb, tile_mean)

    chroma_available = (tile_mean_u is not None and tile_mean_v is not None)

    model_mean = model.mean_y[pb, sb]

    # ── Affine illumination normalization ─────────────────────────────────────────
    # Fit T[i] ≈ a·M[i] + b robustly over background tiles. Drop the worst
    # affine_trim_fraction tiles before refitting so a bird cannot bias the line.
    illum_a, illum_b = 1.0, 0.0
    if cfg.use_affine_normalization:
        valid = model_mean > cfg.affine_model_floor
        n_valid = int(valid.sum())
        if n_valid >= cfg.affine_min_valid_tiles and np.std(model_mean[valid]) > 1.0:
            x = model_mean[valid].ravel()
            t = tile_mean[valid].ravel()
            a, b = np.polyfit(x, t, 1)
            resid = np.abs(t - (a * x + b))
            n_keep = max(10, int(n_valid * (1.0 - cfg.affine_trim_fraction)))
            keep_idx = np.argsort(resid)[:n_keep]
            a, b = np.polyfit(x[keep_idx], t[keep_idx], 1)
            illum_a, illum_b = float(a), float(b)
    bucket_mean = illum_a * model_mean + illum_b

    std = np.sqrt(model.var_y[pb, sb])
    z = np.abs(tile_mean - bucket_mean) / std

    # Dark-only z-anomalous mask (used for QUIET ratio).
    z_mask = z > cfg.tile_z_threshold
    dark_mask = tile_mean < bucket_mean
    mask = z_mask & dark_mask
    total_anom = int(mask.sum())
    ratio = float(mask.mean())

    delta = tile_mean - bucket_mean          # negative = darker than prediction
    tile_delta_luma = -delta                 # positive = darker than model (display convention)
    dark_delta_mask = delta < -cfg.dark_object_min_delta

    if chroma_available:
        du = tile_mean_u - model.mean_u[pb, sb]
        dv = tile_mean_v - model.mean_v[pb, sb]
        if cfg.use_chroma_normalization:
            du = du - np.median(du)      # absorb uniform global colour shift (e.g. sunset warmth)
            dv = dv - np.median(dv)
        chroma_sq = du * du + dv * dv
        chroma_gate_mask = (chroma_sq > cfg.chroma_dark_obj_gate_sq) & dark_mask
        n_chroma_changed = int((chroma_sq > cfg.chroma_dark_obj_gate_sq).sum())
        chroma_delta_max = float(chroma_sq.max())
        dark_tile_mask = dark_delta_mask | chroma_gate_mask
        tile_delta_chroma = np.sqrt(chroma_sq)
    else:
        n_chroma_changed = 0
        chroma_delta_max = 0.0
        dark_tile_mask = dark_delta_mask
        tile_delta_chroma = None

    dark_tiles = int(dark_tile_mask.sum())

    labelled, _ = cc_label(mask)
    if labelled.max() == 0:
        blob_max = 0
    else:
        sizes = np.bincount(labelled.ravel())
        sizes[0] = 0
        blob_max = int(sizes.max())

    # Connected components on the absolute-delta dark mask (8-connected on the tile grid).
    if dark_tiles > 0:
        dark_labelled, _ = cc_label(dark_tile_mask,
                                    structure=np.ones((3, 3), dtype=int))  # 8-connected
        dark_sizes = np.bincount(dark_labelled.ravel())
        dark_sizes[0] = 0
        dark_blob_max = int(dark_sizes.max())
        qualifying = np.where(
            (dark_sizes >= 1) & (dark_sizes <= cfg.dark_blob_max_size)
        )[0]
        dark_blob_mask = np.isin(dark_labelled, qualifying)
    else:
        dark_blob_max = 0
        dark_blob_mask = np.zeros(tile_mean.shape, dtype=bool)

    # ── Texture blob signal ───────────────────────────────────────────────────────
    # Tiles with elevated per-tile Y stddev AND at least loosely darker than the
    # illumination-adjusted prediction form a second compact-blob detector. This
    # catches birds with structured plumage even when dark delta is marginal.
    texture_blob_max = 0
    texture_blob_mask = np.zeros(tile_mean.shape, dtype=bool)
    texture_blob_condition = False
    if tile_std_y is not None and cfg.texture_min_std_y > 0:
        loose_dark_mask = delta < -cfg.texture_dark_delta
        tex_mask = (tile_std_y > cfg.texture_min_std_y) & loose_dark_mask
        if tex_mask.any():
            tex_lab, _ = cc_label(tex_mask, structure=np.ones((3, 3), dtype=int))
            tex_sizes = np.bincount(tex_lab.ravel())
            tex_sizes[0] = 0
            texture_blob_max = int(tex_sizes.max())
            q_tex = np.where((tex_sizes >= 1) & (tex_sizes <= cfg.dark_blob_max_size))[0]
            texture_blob_mask = np.isin(tex_lab, q_tex)
            texture_blob_condition = (
                texture_blob_max >= 1 and texture_blob_max <= cfg.dark_blob_max_size
            )

    compactness = blob_max / total_anom if total_anom > 0 else 0.0
    warmup = model.warmup_remaining(pb, sb) > 0

    dark_blob_condition = (
        dark_tiles >= cfg.dark_object_min_tiles
        and dark_blob_max >= 1
        and dark_blob_max <= cfg.dark_blob_max_size
    )

    # Merge dark_blob_mask with texture_blob_mask for display overlay.
    combined_blob_mask = dark_blob_mask | texture_blob_mask

    if warmup:
        trigger = "WARMUP"
        decision = "process"
        reason = f"cell warmup ({model.warmup_remaining(pb, sb)} more obs needed) → lean upload"
    elif dark_blob_condition or texture_blob_condition:
        # DARK_BLOB before QUIET: a compact bird-sized cluster must be uploaded even
        # on an otherwise calm background (ratio can be near zero when a bird lands
        # on a well-matched scene — QUIET would wrongly suppress it).
        trigger = "DARK_BLOB"
        decision = "process"
        reason = (f"compact dark blob (dark_tiles={dark_tiles}, blob_max={dark_blob_max}, "
                  f"texture_blob_max={texture_blob_max}, "
                  f"chroma_changed={n_chroma_changed}, dark_ratio={ratio:.2f}, "
                  f"photo_bucket={pb_name})")
    elif ratio <= cfg.quiet_anomaly_ratio:
        trigger = "QUIET"
        decision = "clouds"
        reason = (f"scene matches model (dark_ratio={ratio:.3f} ≤ {cfg.quiet_anomaly_ratio}, "
                  f"photo_bucket={pb_name})")
    else:
        trigger = "AMBIGUOUS"
        decision = "process"
        reason = (f"ambiguous → upload (dark_tiles={dark_tiles}, blob_max={dark_blob_max}, "
                  f"dark_ratio={ratio:.2f}, compactness={compactness:.2f}, "
                  f"photo_bucket={pb_name})")

    return ClassifierResult(
        label=decision,
        trigger=trigger,
        anomaly_mask=mask,
        blob_max_size=blob_max,
        dark_blob_max=dark_blob_max,
        anomaly_ratio=ratio,
        compactness=compactness,
        reason=reason,
        warmup=warmup,
        dark_tile_mask=dark_tile_mask,
        dark_blob_mask=combined_blob_mask,
        tile_delta_luma=tile_delta_luma,
        dark_tiles=dark_tiles,
        photo_bucket=pb_name,
        scene_bucket=sb,
        n_chroma_changed=n_chroma_changed,
        chroma_delta_max=chroma_delta_max,
        tile_delta_chroma=tile_delta_chroma,
        illum_a=illum_a,
        illum_b=illum_b,
        texture_blob_max=texture_blob_max,
    )
