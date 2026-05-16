"""Systematic parameter sweep + stage ablation for the cloud-check classifier.

Goal: find configurations that minimise missed birds (FN) while maximising
cloud filtering (TN), and identify stages that pull their weight.

Approach:
- Decode every training JPEG ONCE and cache its tile-mean feature tensor.
- For each config, simulate the full online pipeline (model updates included)
  over the cached features. The hot inner loop is pure numpy, no I/O.
- Report:
    1. The current production config (baseline).
    2. Top configs by Pareto criterion: prefer FN==0; among ties, max TN.
    3. Stage-ablation table: disable QUIET / DIFFUSE / DARK_OBJ / SCENE_DRIFT
       individually and observe the swing on each metric.

Usage (from /workspace/src/cloud-check):
    .venv/bin/python -m scripts.sweep
"""

from __future__ import annotations

import itertools
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_check.background import BackgroundModel
from cloud_check.config import Config
from cloud_check.dataset import load_dataset
from cloud_check.features import extract_tile_features, load_gray_vga

# ---------------------------------------------------------------------------
# Stage flags: lets us ablate each rule by flipping a bit, without touching
# the production classifier.
# ---------------------------------------------------------------------------

ALL_STAGES = ("NIGHT", "WARMUP", "DARK_OBJ", "QUIET", "SCENE_DRIFT", "AMBIGUOUS")


def classify_inline(
    tile_mean: np.ndarray,
    hour: int,
    model: BackgroundModel,
    cfg: Config,
    prev_tile_mean: np.ndarray | None,
    enabled: frozenset[str],
) -> tuple[str, str]:
    """Identical to cloud_check.classifier.classify but supports stage ablation
    and returns only what the sweep needs: (label, trigger)."""

    # Stage 0 — NIGHT
    if "NIGHT" in enabled and cfg.night_brightness_threshold > 0:
        if tile_mean.mean() < cfg.night_brightness_threshold:
            return "non-cloud", "NIGHT"

    bucket = model._idx(hour)
    z = np.abs(tile_mean - model.mean[bucket]) / np.sqrt(model.var[bucket])
    mask = z > cfg.tile_z_threshold
    ratio = float(mask.mean())

    delta = tile_mean - model.mean[bucket]
    dark_tiles = int(((delta < -cfg.dark_object_min_delta) & mask).sum())

    if prev_tile_mean is not None:
        temporal_delta = tile_mean - prev_tile_mean
        new_dark_tiles = int(((temporal_delta < -cfg.temporal_dark_delta) & mask).sum())
        temporal_available = True
    else:
        new_dark_tiles = dark_tiles
        temporal_available = False

    warmup = model.warmup_remaining(hour) > 0

    dark_obj_fires = (
        "DARK_OBJ" in enabled
        and dark_tiles >= cfg.dark_object_min_tiles
        and (not temporal_available or new_dark_tiles >= cfg.dark_object_min_tiles)
    )
    stale_fires = (
        "SCENE_DRIFT" in enabled
        and dark_tiles >= cfg.dark_object_min_tiles
        and temporal_available
        and new_dark_tiles < cfg.dark_object_min_tiles
    )

    if warmup and "WARMUP" in enabled:
        return "non-cloud", "WARMUP"
    if dark_obj_fires:
        return "non-cloud", "DARK_OBJ"
    if "QUIET" in enabled and ratio <= cfg.quiet_anomaly_ratio:
        return "cloud", "QUIET"
    if stale_fires:
        return "non-cloud", "SCENE_DRIFT"
    return "non-cloud", "AMBIGUOUS"


# ---------------------------------------------------------------------------
# Cached pipeline
# ---------------------------------------------------------------------------

@dataclass
class CachedSample:
    label: str
    hour: int
    tile_mean: np.ndarray
    filename: str


def load_cache() -> list[CachedSample]:
    raw = [s for s in load_dataset() if s.domain != "aux-2025"]
    raw.sort(key=lambda s: (s.taken_at.isoformat() if s.taken_at else s.path.name))
    cache: list[CachedSample] = []
    for s in raw:
        frame = load_gray_vga(s.path)
        feats = extract_tile_features(frame)
        cache.append(CachedSample(
            label=s.label,
            hour=s.hour_bucket,
            tile_mean=feats["mean"],
            filename=s.path.name,
        ))
    return cache


def evaluate(cfg: Config, cache: list[CachedSample],
             enabled: frozenset[str] = frozenset(ALL_STAGES)) -> dict:
    """Run the online pipeline over the cached features. Returns metrics."""
    model = BackgroundModel(cfg)
    prev_mean: dict[int, np.ndarray] = {}
    tp = fn = fp = tn = 0
    trigger_counts: dict[str, int] = {}

    for s in cache:
        bucket = model._idx(s.hour)
        prev = prev_mean.get(bucket)
        was_warmup = model.warmup_remaining(s.hour) > 0
        model.observe(s.hour)
        label, trigger = classify_inline(s.tile_mean, s.hour, model, cfg, prev, enabled)
        trigger_counts[trigger] = trigger_counts.get(trigger, 0) + 1

        # Decision matrix (label "non-cloud" = upload, "cloud" = suppress).
        if s.label == "non-cloud" and label == "non-cloud":
            tp += 1
        elif s.label == "non-cloud" and label == "cloud":
            fn += 1
        elif s.label == "cloud" and label == "non-cloud":
            fp += 1
        else:
            tn += 1

        # Update policy — mirrors production:
        # - warmup: always (bootstrap)
        # - cloud prediction: always
        # - SCENE_DRIFT: yes (model is stale)
        if was_warmup or label == "cloud" or trigger in ("SCENE_DRIFT", "NIGHT"):
            model.update(s.hour, s.tile_mean)
        prev_mean[bucket] = s.tile_mean

    nc_recall = tp / (tp + fn) if (tp + fn) else 0.0
    c_recall = tn / (tn + fp) if (tn + fp) else 0.0
    return {
        "TP": tp, "FN": fn, "FP": fp, "TN": tn,
        "nc_recall": nc_recall, "c_recall": c_recall,
        "triggers": trigger_counts,
    }


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

def grid() -> Iterable[Config]:
    """Focused grid targeting the z_threshold / QUIET boundary.

    The FN=2 case (wrong_night person + pillow) fails because high sky-tile
    variance (model_std_avg ≈ 36) keeps z-scores below 3.0 even when the
    absolute delta vs model is 120+.  Lowering tile_z_threshold is the fix;
    quiet_anomaly_ratio may need to rise to compensate (more tiles flagged
    as anomalous in lighting frames).

    Total = 3 * 4 * 5 * 4 * 3 = 720 configs (~25 s on cached features).
    """
    base = Config()
    for night_thresh in [70.0, 80.0, 90.0]:
        for tile_z in [1.5, 2.0, 2.5, 3.0]:
            for q_ratio in [0.05, 0.10, 0.15, 0.20, 0.25]:
                for d_delta in [15.0, 20.0, 25.0, 30.0]:
                    for t_delta in [5.0, 10.0, 15.0]:
                        yield replace(
                            base,
                            night_brightness_threshold=night_thresh,
                            tile_z_threshold=tile_z,
                            quiet_anomaly_ratio=q_ratio,
                            dark_object_min_delta=d_delta,
                            temporal_dark_delta=t_delta,
                        )


def cfg_summary(cfg: Config) -> str:
    return (f"night={cfg.night_brightness_threshold} z={cfg.tile_z_threshold} "
            f"q={cfg.quiet_anomaly_ratio} dδ={cfg.dark_object_min_delta} "
            f"tδ={cfg.temporal_dark_delta} α={cfg.ema_alpha} "
            f"warm={cfg.warmup_frames_per_bucket}")


def print_row(tag: str, r: dict, cfg_str: str = "") -> None:
    print(f"  {tag:24s}  TP={r['TP']:3d} FN={r['FN']:3d} FP={r['FP']:3d} TN={r['TN']:3d}  "
          f"nc_recall={r['nc_recall']:.3f}  c_recall={r['c_recall']:.3f}  {cfg_str}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Caching tile features...")
    t0 = time.time()
    cache = load_cache()
    print(f"  cached {len(cache)} frames in {time.time()-t0:.1f}s")

    # 1. Production baseline
    base_cfg = Config()
    print("\n--- BASELINE (current production Config) ---")
    base_r = evaluate(base_cfg, cache)
    print_row("baseline", base_r, cfg_summary(base_cfg))
    print(f"    triggers: {dict(sorted(base_r['triggers'].items()))}")

    # 2. Ablations on baseline config
    print("\n--- STAGE ABLATIONS (baseline config, one rule disabled) ---")
    for stage in ALL_STAGES:
        enabled = frozenset(s for s in ALL_STAGES if s != stage)
        r = evaluate(base_cfg, cache, enabled)
        delta_fn = r["FN"] - base_r["FN"]
        delta_tn = r["TN"] - base_r["TN"]
        delta_str = f"ΔFN={delta_fn:+d}  ΔTN={delta_tn:+d}"
        print_row(f"NO {stage:<11s}", r, delta_str)

    # 3. Full parameter grid
    print("\n--- PARAMETER GRID SWEEP ---")
    grid_list = list(grid())
    print(f"  evaluating {len(grid_list)} configs...")
    t0 = time.time()
    results: list[tuple[Config, dict]] = []
    for i, cfg in enumerate(grid_list):
        r = evaluate(cfg, cache)
        results.append((cfg, r))
        if (i + 1) % 1000 == 0:
            pace = (i + 1) / (time.time() - t0)
            eta = (len(grid_list) - i - 1) / pace
            print(f"    {i+1}/{len(grid_list)}  ({pace:.0f}/s, ETA {eta:.0f}s)")
    print(f"  done in {time.time()-t0:.1f}s")

    # Pareto: prefer FN == 0; among those, max TN.
    fn_zero = [(c, r) for c, r in results if r["FN"] == 0]
    print(f"\n  configs with FN==0 (zero missed birds): {len(fn_zero)} / {len(results)}")

    print("\n--- TOP 10 BY FN==0 then max TN ---")
    fn_zero.sort(key=lambda cr: (-cr[1]["TN"], cr[1]["FP"]))
    for cfg, r in fn_zero[:10]:
        print_row("", r, cfg_summary(cfg))

    print("\n--- TOP 10 BY MAX TN OVERALL (any FN) ---")
    by_tn = sorted(results, key=lambda cr: (-cr[1]["TN"], cr[1]["FN"]))
    for cfg, r in by_tn[:10]:
        print_row("", r, cfg_summary(cfg))

    print("\n--- TOP 10 BY MIN FN (most birds caught), ties broken by TN ---")
    by_fn = sorted(results, key=lambda cr: (cr[1]["FN"], -cr[1]["TN"]))
    for cfg, r in by_fn[:10]:
        print_row("", r, cfg_summary(cfg))


if __name__ == "__main__":
    main()
