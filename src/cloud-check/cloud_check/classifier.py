from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import label as cc_label

from .background import BackgroundModel
from .config import Config


@dataclass
class ClassifierResult:
    label: str                    # "clouds" or "process"
    trigger: str                  # rule: WARMUP | DARK_OBJ | QUIET | SCENE_DRIFT | AMBIGUOUS
    anomaly_mask: np.ndarray      # (GRID_H, GRID_W) bool — tiles with z > threshold AND darker than model
    blob_max_size: int            # largest connected anomalous region
    anomaly_ratio: float          # dark-only anomaly ratio (tiles darker than model / total)
    compactness: float            # blob_max / total_anomalies (0..1, 1 = single solid blob)
    reason: str                   # human-readable explanation
    warmup: bool                  # bucket is still in warmup
    new_dark_tiles: int           # tiles newly dark vs previous frame (dark_tiles if no prev)
    temporal_available: bool      # whether prev_tile_mean was provided


def classify(
    tile_mean: np.ndarray,
    hour: int,
    model: BackgroundModel,
    cfg: Config | None = None,
    prev_tile_mean: np.ndarray | None = None,
) -> ClassifierResult:
    """Decision rule biased toward 'process' (upload-anyway).

    Sending one extra photo is cheap; missing a bird/person is the failure
    we minimise. The only path that returns 'clouds' (and thus suppresses upload)
    is QUIET: the scene is almost identical to the bucket model. Everything
    else — DARK_OBJ, SCENE_DRIFT, WARMUP, AMBIGUOUS — uploads.

    (A previous DIFFUSE rule for "global lighting shift" was removed: the grid
    sweep in scripts/sweep.py showed it never fires on real data, because such
    shifts always also create dark tiles → caught by DARK_OBJ or SCENE_DRIFT.)
    """

    cfg = cfg or model.cfg

    # Stage 0 — NIGHT: frame too dark for reliable anomaly detection → upload.
    global_mean = float(tile_mean.mean())
    if cfg.night_brightness_threshold > 0 and global_mean < cfg.night_brightness_threshold:
        return ClassifierResult(
            label="process",
            trigger="NIGHT",
            anomaly_mask=np.zeros(tile_mean.shape, dtype=bool),
            blob_max_size=0,
            anomaly_ratio=0.0,
            compactness=0.0,
            reason=f"scene too dark (global_mean={global_mean:.1f} < {cfg.night_brightness_threshold})",
            warmup=model.warmup_remaining(hour) > 0,
            new_dark_tiles=0,
            temporal_available=prev_tile_mean is not None,
        )

    z = model.z_scores(hour, tile_mean)
    bucket_mean = model.mean[model._idx(hour)]

    # anomaly_mask: tiles with z > threshold AND darker than model.
    # Bright deviations (sky brightening, cloud moving off sun) are intentionally
    # excluded — they should not prevent QUIET from suppressing.
    z_mask = z > cfg.tile_z_threshold
    dark_mask = tile_mean < bucket_mean
    mask = z_mask & dark_mask          # dark-only anomalous tiles
    total_anom = int(mask.sum())
    ratio = float(mask.mean())         # dark-only ratio used for QUIET

    delta = tile_mean - bucket_mean
    dark_tiles = int(((delta < -cfg.dark_object_min_delta) & z_mask).sum())

    if prev_tile_mean is not None:
        temporal_delta = tile_mean - prev_tile_mean
        new_dark_tiles = int(((temporal_delta < -cfg.temporal_dark_delta) & z_mask).sum())
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

    compactness = blob_max / total_anom if total_anom > 0 else 0.0
    warmup = model.warmup_remaining(hour) > 0

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
        reason = f"bucket warmup ({model.warmup_remaining(hour)} more obs needed) → lean upload"
    elif dark_obj_condition:
        trigger = "DARK_OBJ"
        decision = "process"
        reason = (f"dark object cue (dark_tiles={dark_tiles}, new_dark={new_dark_tiles}, "
                  f"blob={blob_max}, dark_ratio={ratio:.2f})")
    elif ratio <= cfg.quiet_anomaly_ratio:
        trigger = "QUIET"
        decision = "clouds"
        reason = f"scene matches model (dark_ratio={ratio:.3f} ≤ {cfg.quiet_anomaly_ratio})"
    elif stale_condition:
        trigger = "SCENE_DRIFT"
        decision = "process"
        reason = (f"persistent scene drift (dark_tiles={dark_tiles}, new_dark=0, "
                  f"dark_ratio={ratio:.2f}) → model stale, upload + re-calibrate")
    else:
        trigger = "AMBIGUOUS"
        decision = "process"
        reason = (f"ambiguous → upload (blob={blob_max} dark_ratio={ratio:.2f} "
                  f"compactness={compactness:.2f})")

    return ClassifierResult(
        label=decision,
        trigger=trigger,
        anomaly_mask=mask,
        blob_max_size=blob_max,
        anomaly_ratio=ratio,
        compactness=compactness,
        reason=reason,
        warmup=warmup,
        new_dark_tiles=new_dark_tiles,
        temporal_available=temporal_available,
    )
