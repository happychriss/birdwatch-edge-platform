from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import label as cc_label

from .background import BackgroundModel
from .config import Config


@dataclass
class ClassifierResult:
    label: str                    # "cloud" or "non-cloud"
    trigger: str                  # rule: WARMUP | DARK_OBJ | QUIET | SCENE_DRIFT | AMBIGUOUS
    anomaly_mask: np.ndarray      # (GRID_H, GRID_W) bool
    blob_max_size: int            # largest connected anomalous region
    anomaly_ratio: float          # fraction of tiles anomalous
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
    """Decision rule biased toward 'non-cloud' (upload-anyway).

    Sending one extra photo is cheap; missing a bird/person is the failure
    we minimise. The only path that returns 'cloud' (and thus suppresses upload)
    is QUIET: the scene is almost identical to the bucket model. Everything
    else — DARK_OBJ, SCENE_DRIFT, WARMUP, AMBIGUOUS — uploads.

    (A previous DIFFUSE rule for "global lighting shift" was removed: the grid
    sweep in scripts/sweep.py showed it never fires on real data, because such
    shifts always also create dark tiles → caught by DARK_OBJ or SCENE_DRIFT.)
    """

    cfg = cfg or model.cfg

    z = model.z_scores(hour, tile_mean)
    mask = z > cfg.tile_z_threshold
    total_anom = int(mask.sum())
    ratio = float(mask.mean())

    # Foreground darkening signal: birds/people are typically darker than the
    # bright sky/floor background. A handful of tiles that dropped a lot below
    # their bucket mean is a strong object cue even when total_anom is tiny.
    bucket_mean = model.mean[model._idx(hour)]
    delta = tile_mean - bucket_mean
    dark_tiles = int(((delta < -cfg.dark_object_min_delta) & mask).sum())

    # Temporal check: which dark tiles are genuinely NEW vs the previous frame?
    # If they were already dark in the previous frame, the model is stale (scene
    # changed since last update), not a new object.
    if prev_tile_mean is not None:
        temporal_delta = tile_mean - prev_tile_mean
        new_dark_tiles = int(((temporal_delta < -cfg.temporal_dark_delta) & mask).sum())
        temporal_available = True
    else:
        new_dark_tiles = dark_tiles  # no previous frame: trust model-based count
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

    # DARK_OBJ fires only when tiles are dark AND newly appeared (or no prev to check).
    # If the dark tiles were already there in the previous frame, the model is stale —
    # that becomes SCENE_DRIFT below.
    dark_obj_condition = (
        dark_tiles >= cfg.dark_object_min_tiles
        and (not temporal_available or new_dark_tiles >= cfg.dark_object_min_tiles)
    )
    stale_condition = (
        dark_tiles >= cfg.dark_object_min_tiles
        and temporal_available
        and new_dark_tiles < cfg.dark_object_min_tiles
    )

    if warmup:
        trigger = "WARMUP"
        decision = "non-cloud"
        reason = f"bucket warmup ({model.warmup_remaining(hour)} more obs needed) → lean upload"
    elif dark_obj_condition:
        trigger = "DARK_OBJ"
        decision = "non-cloud"
        reason = (f"dark object cue (dark_tiles={dark_tiles}, new_dark={new_dark_tiles}, "
                  f"blob={blob_max}, ratio={ratio:.2f})")
    elif ratio <= cfg.quiet_anomaly_ratio:
        trigger = "QUIET"
        decision = "cloud"
        reason = f"scene matches model (ratio={ratio:.3f} ≤ {cfg.quiet_anomaly_ratio})"
    elif stale_condition:
        # Dark tiles vs model but none of them are newly dark vs previous frame.
        # The scene drifted (items moved, sun angle, plants) and the model is stale.
        # Upload (safety bias) but signal that the model should update from this frame.
        trigger = "SCENE_DRIFT"
        decision = "non-cloud"
        reason = (f"persistent scene drift (dark_tiles={dark_tiles}, new_dark=0, "
                  f"ratio={ratio:.2f}) → model stale, upload + re-calibrate")
    else:
        trigger = "AMBIGUOUS"
        decision = "non-cloud"
        reason = (f"ambiguous → upload (blob={blob_max} ratio={ratio:.2f} "
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
