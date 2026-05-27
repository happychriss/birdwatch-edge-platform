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


def classify(
    tile_mean: np.ndarray,
    model: BackgroundModel,
    cfg: Config | None = None,
    tile_mean_u: np.ndarray | None = None,
    tile_mean_v: np.ndarray | None = None,
) -> ClassifierResult:
    """Background-model classifier (Layer-2). Called only on frames that passed Layer-1.

    NIGHT is handled by the burst pre-filter (Layer-1); this function never sees dark frames.
    Stages in priority order:
        WARMUP    — bucket cell still bootstrapping → process
        QUIET     — scene matches model (ratio ≤ threshold) → suppress
        DARK_BLOB — compact dark cluster (1..dark_blob_max_size tiles) → process (bird)
        AMBIGUOUS — default → process (safety bias)

    Chroma (U, V): when provided, a tile qualifies as dark if Y drop ≥ threshold OR
    (chroma shift vs model is large AND tile is darker on Y). QUIET ratio is Y-only.
    """

    cfg = cfg or model.cfg

    global_mean = int(tile_mean.mean())   # truncate, matches ESP integer division
    pb_name = model.photo_bucket_for(global_mean)
    pb = photo_bucket_idx(pb_name)
    sb = model.scene_bucket_for(pb, tile_mean)

    chroma_available = (tile_mean_u is not None and tile_mean_v is not None)

    z = model.z_scores_y(pb, sb, tile_mean)
    bucket_mean = model.mean_y[pb, sb]

    # Dark-only z-anomalous mask (used for QUIET ratio).
    z_mask = z > cfg.tile_z_threshold
    dark_mask = tile_mean < bucket_mean
    mask = z_mask & dark_mask
    total_anom = int(mask.sum())
    ratio = float(mask.mean())

    delta = tile_mean - bucket_mean          # negative = darker than model
    tile_delta_luma = -delta                 # positive = darker than model (display convention)
    dark_delta_mask = delta < -cfg.dark_object_min_delta

    if chroma_available:
        chroma_sq = model.chroma_delta_sq(pb, sb, tile_mean_u, tile_mean_v)
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
    # Builds dark_blob_mask: tiles that are part of a qualifying compact blob (1..dark_blob_max_size).
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

    compactness = blob_max / total_anom if total_anom > 0 else 0.0
    warmup = model.warmup_remaining(pb, sb) > 0

    dark_blob_condition = (
        dark_tiles >= cfg.dark_object_min_tiles
        and dark_blob_max >= 1
        and dark_blob_max <= cfg.dark_blob_max_size
    )

    if warmup:
        trigger = "WARMUP"
        decision = "process"
        reason = f"cell warmup ({model.warmup_remaining(pb, sb)} more obs needed) → lean upload"
    elif ratio <= cfg.quiet_anomaly_ratio:
        trigger = "QUIET"
        decision = "clouds"
        reason = (f"scene matches model (dark_ratio={ratio:.3f} ≤ {cfg.quiet_anomaly_ratio}, "
                  f"photo_bucket={pb_name})")
    elif dark_blob_condition:
        trigger = "DARK_BLOB"
        decision = "process"
        reason = (f"compact dark blob (dark_tiles={dark_tiles}, blob_max={dark_blob_max}, "
                  f"chroma_changed={n_chroma_changed}, dark_ratio={ratio:.2f}, "
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
        dark_blob_mask=dark_blob_mask,
        tile_delta_luma=tile_delta_luma,
        dark_tiles=dark_tiles,
        photo_bucket=pb_name,
        scene_bucket=sb,
        n_chroma_changed=n_chroma_changed,
        chroma_delta_max=chroma_delta_max,
        tile_delta_chroma=tile_delta_chroma,
    )
