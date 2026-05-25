from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import label as cc_label

from .background import BackgroundModel, photo_bucket_idx
from .config import Config


@dataclass
class ClassifierResult:
    label: str                    # "clouds" or "process"
    trigger: str                  # WARMUP | DARK_OBJ | QUIET | SCENE_DRIFT | AMBIGUOUS | NIGHT
    anomaly_mask: np.ndarray      # (GRID_H, GRID_W) bool — tiles with z > threshold AND darker than model
    blob_max_size: int            # largest connected anomalous region (z-mask)
    dark_blob_max: int            # largest connected blob on absolute-delta dark mask
    anomaly_ratio: float          # dark-only anomaly ratio (tiles darker than model / total)
    compactness: float            # blob_max / total_anomalies (0..1, 1 = single solid blob)
    reason: str                   # human-readable explanation
    warmup: bool                  # cell is still in warmup
    new_dark_tiles: int           # tiles newly dark vs previous frame (dark_tiles if no prev)
    temporal_available: bool      # whether prev_tile_mean was provided
    dark_tiles: int = 0           # tiles ≥ dark_object_min_delta darker than model (no z-gate)
    photo_bucket: str = "NORMAL"  # outer bucket name (NORMAL | BRIGHT | LOWLIGHT)
    scene_bucket: int = 0         # inner bucket index (0..K_scene-1; today always 0)
    n_chroma_changed: int = 0     # tiles where chroma differs from model beyond gate (0 if no chroma)
    chroma_delta_max: float = 0.0 # max ΔU²+ΔV² across all tiles (0 if no chroma) — diagnostic


def classify(
    tile_mean: np.ndarray,
    model: BackgroundModel,
    cfg: Config | None = None,
    prev_tile_mean: np.ndarray | None = None,
    tile_mean_u: np.ndarray | None = None,
    tile_mean_v: np.ndarray | None = None,
) -> ClassifierResult:
    """Bias-toward-"process" classifier for the background-model pipeline.

    Bucket selection: photo-bucket from global Y mean (NORMAL/BRIGHT/LOWLIGHT),
    scene-bucket nearest-centroid within that photo-bucket (always 0 today at K_scene=1).
    Stages in order of priority:
        NIGHT       — frame too dark for reliable anomaly detection → process
        WARMUP      — bucket cell still bootstrapping → process
        DARK_OBJ    — compact dark-on-Y tile (or dark + chroma-different) appeared → process
        SCENE_DRIFT — many persistently-dark tiles but no new ones → process + re-calibrate
        QUIET       — scene matches model → suppress as clouds
        AMBIGUOUS   — default → process (safety bias)

    Chroma (U, V) integration: when provided, DARK_OBJ counts a tile as
    object-like if either the Y delta is large OR the chroma delta vs model is
    large. The QUIET ratio remains Y-only (ambient illumination shifts must not
    prevent suppression).
    """

    cfg = cfg or model.cfg

    # Photo-bucket from global Y mean. Scene-bucket inside that photo-bucket
    # (K_scene=1 today → always 0).
    global_mean = int(tile_mean.mean())   # truncate, matches ESP integer division
    pb_name = model.photo_bucket_for(global_mean)
    pb = photo_bucket_idx(pb_name)
    sb = model.scene_bucket_for(pb, tile_mean)

    chroma_available = (tile_mean_u is not None and tile_mean_v is not None)

    # Stage 0 — NIGHT
    if cfg.night_brightness_threshold > 0 and global_mean < cfg.night_brightness_threshold:
        return ClassifierResult(
            label="process",
            trigger="NIGHT",
            anomaly_mask=np.zeros(tile_mean.shape, dtype=bool),
            blob_max_size=0,
            dark_blob_max=0,
            anomaly_ratio=0.0,
            compactness=0.0,
            reason=f"scene too dark (global_mean={global_mean} < {cfg.night_brightness_threshold})",
            warmup=model.warmup_remaining(pb, sb) > 0,
            new_dark_tiles=0,
            temporal_available=prev_tile_mean is not None,
            dark_tiles=0,
            photo_bucket=pb_name,
            scene_bucket=sb,
            n_chroma_changed=0,
            chroma_delta_max=0.0,
        )

    z = model.z_scores_y(pb, sb, tile_mean)
    bucket_mean = model.mean_y[pb, sb]

    # Dark-only z-anomalous mask (used for QUIET ratio). Bright deviations
    # (sky brightening, cloud moving off sun) are excluded so they do not
    # prevent QUIET suppression.
    z_mask = z > cfg.tile_z_threshold
    dark_mask = tile_mean < bucket_mean
    mask = z_mask & dark_mask
    total_anom = int(mask.sum())
    ratio = float(mask.mean())

    delta = tile_mean - bucket_mean
    # No z-gate on dark_tiles — just absolute delta vs model.
    dark_delta_mask = delta < -cfg.dark_object_min_delta
    dark_tiles = int(dark_delta_mask.sum())

    # Chroma-gate mask: ΔU² + ΔV² > chroma_dark_obj_gate_sq. Used to admit
    # tiles into DARK_OBJ even when Y delta is below dark_object_min_delta,
    # *provided* the tile is darker than the model on Y (so a brightening
    # tile that just happens to shift in chroma cannot trigger DARK_OBJ).
    if chroma_available:
        chroma_sq = model.chroma_delta_sq(pb, sb, tile_mean_u, tile_mean_v)
        chroma_gate_mask = (chroma_sq > cfg.chroma_dark_obj_gate_sq) & dark_mask
        n_chroma_changed = int((chroma_sq > cfg.chroma_dark_obj_gate_sq).sum())
        chroma_delta_max = float(chroma_sq.max())
        # Final dark-tile mask combines absolute-Y-delta OR chroma-gate (both gated on dark_mask).
        dark_tile_mask = dark_delta_mask | chroma_gate_mask
        dark_tiles = int(dark_tile_mask.sum())
    else:
        chroma_sq = None
        n_chroma_changed = 0
        chroma_delta_max = 0.0
        dark_tile_mask = dark_delta_mask

    if prev_tile_mean is not None:
        temporal_delta = tile_mean - prev_tile_mean
        new_dark_tiles = int((temporal_delta < -cfg.temporal_dark_delta).sum())
        temporal_available = True
    else:
        new_dark_tiles = dark_tiles
        temporal_available = False

    labelled, _ = cc_label(mask)
    if labelled.max() == 0:
        blob_max = 0
    else:
        sizes = np.bincount(labelled.ravel())
        sizes[0] = 0
        blob_max = int(sizes.max())

    # Blob on absolute-delta dark mask — distinguishes compact bird from diffuse shadow.
    if dark_tiles > 0:
        dark_labelled, _ = cc_label(dark_tile_mask)
        dark_sizes = np.bincount(dark_labelled.ravel())
        dark_sizes[0] = 0
        dark_blob_max = int(dark_sizes.max())
    else:
        dark_blob_max = 0
    compactness = blob_max / total_anom if total_anom > 0 else 0.0
    warmup = model.warmup_remaining(pb, sb) > 0

    dark_obj_condition = (
        dark_tiles >= cfg.dark_object_min_tiles
        and (not temporal_available or new_dark_tiles >= cfg.dark_object_min_tiles)
    )
    stale_condition = (
        dark_tiles >= cfg.scene_drift_min_tiles
        and temporal_available
        and new_dark_tiles < cfg.dark_object_min_tiles
    )

    if warmup:
        trigger = "WARMUP"
        decision = "process"
        reason = f"cell warmup ({model.warmup_remaining(pb, sb)} more obs needed) → lean upload"
    elif dark_obj_condition:
        trigger = "DARK_OBJ"
        decision = "process"
        reason = (f"dark object cue (dark_tiles={dark_tiles}, new_dark={new_dark_tiles}, "
                  f"blob={blob_max}, dark_ratio={ratio:.2f}, "
                  f"chroma_changed={n_chroma_changed}, photo_bucket={pb_name})")
    elif stale_condition:
        # Check stale BEFORE quiet: high dark_tiles with no new change means the model
        # hasn't caught up with a gradual scene shift — upload and re-calibrate.
        trigger = "SCENE_DRIFT"
        decision = "process"
        reason = (f"persistent scene drift (dark_tiles={dark_tiles}, new_dark=0, "
                  f"dark_ratio={ratio:.2f}) → model stale, upload + re-calibrate")
    elif ratio <= cfg.quiet_anomaly_ratio:
        trigger = "QUIET"
        decision = "clouds"
        reason = (f"scene matches model (dark_ratio={ratio:.3f} ≤ {cfg.quiet_anomaly_ratio}, "
                  f"photo_bucket={pb_name})")
    else:
        trigger = "AMBIGUOUS"
        decision = "process"
        reason = (f"ambiguous → upload (blob={blob_max} dark_ratio={ratio:.2f} "
                  f"compactness={compactness:.2f}, photo_bucket={pb_name})")

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
        new_dark_tiles=new_dark_tiles,
        temporal_available=temporal_available,
        dark_tiles=dark_tiles,
        photo_bucket=pb_name,
        scene_bucket=sb,
        n_chroma_changed=n_chroma_changed,
        chroma_delta_max=chroma_delta_max,
    )
