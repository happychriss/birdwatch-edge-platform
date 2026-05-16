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

ALL_STAGES = ("NIGHT", "WARMUP", "DARK_OBJ", "INDIRECT_LIGHT", "SPOT_CHANGE", "QUIET", "SCENE_DRIFT", "AMBIGUOUS")


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
            return "process", "NIGHT"

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
        and dark_tiles >= cfg.scene_drift_min_tiles
        and temporal_available
        and new_dark_tiles < cfg.dark_object_min_tiles
    )

    if warmup and "WARMUP" in enabled:
        return "process", "WARMUP"
    if dark_obj_fires:
        return "process", "DARK_OBJ"
    if ("INDIRECT_LIGHT" in enabled and cfg.indirect_light_threshold > 0
            and float(tile_mean.mean()) < cfg.indirect_light_threshold):
        return "process", "INDIRECT_LIGHT"
    if (
        "SPOT_CHANGE" in enabled
        and prev_tile_mean is not None
        and cfg.spot_change_max_tiles > 0
        and abs(float(tile_mean.mean()) - float(prev_tile_mean.mean())) < cfg.spot_change_global_stability
        and 1 <= int((tile_mean - prev_tile_mean < -cfg.spot_change_tile_delta).sum()) <= cfg.spot_change_max_tiles
        and int((np.abs(tile_mean - prev_tile_mean) >= 10).sum()) <= cfg.spot_change_max_noisy_tiles
    ):
        return "process", "SPOT_CHANGE"
    if "QUIET" in enabled and ratio <= cfg.quiet_anomaly_ratio:
        return "clouds", "QUIET"
    if stale_fires:
        return "process", "SCENE_DRIFT"
    return "process", "AMBIGUOUS"


# ---------------------------------------------------------------------------
# Cached pipeline
# ---------------------------------------------------------------------------

@dataclass
class CachedSample:
    label: str
    hour: int
    tile_mean: np.ndarray
    filename: str


def load_cache(grid_w: int = 16, grid_h: int = 12) -> list[CachedSample]:
    raw = [s for s in load_dataset() if s.domain != "aux-2025"]
    raw.sort(key=lambda s: (s.taken_at.isoformat() if s.taken_at else s.path.name))
    cache: list[CachedSample] = []
    for s in raw:
        frame = load_gray_vga(s.path)
        feats = extract_tile_features(frame, grid_w=grid_w, grid_h=grid_h)
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

        # Decision matrix (label "process" = upload, "clouds" = suppress).
        if s.label == "process" and label == "process":
            tp += 1
        elif s.label == "process" and label == "clouds":
            fn += 1
        elif s.label == "clouds" and label == "process":
            fp += 1
        else:
            tn += 1

        # Update policy — mirrors production:
        # - warmup: always (bootstrap)
        # - clouds prediction: always
        # - SCENE_DRIFT / NIGHT / INDIRECT_LIGHT: yes (model tracks baseline)
        # - SPOT_CHANGE / DARK_OBJ / AMBIGUOUS: no (possible object → don't pollute model)
        if was_warmup or label == "clouds" or trigger in ("SCENE_DRIFT", "NIGHT", "INDIRECT_LIGHT"):
            model.update(s.hour, s.tile_mean)
        if trigger == "SCENE_DRIFT":
            model.reset_warmup(s.hour)
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
    """Grid exploring INDIRECT_LIGHT, SPOT_CHANGE, and lightcheck resolution.

    indirect_light_threshold=0 / spot_change_max_tiles=0 disables those stages.
    grid (16×12) = QQVGA lightcheck; (32×24) = QVGA lightcheck.

    Total = 2 * 4 * 3 * 3 * 4 * 3 * 2 * 3 = 5184 configs (~2 min on cached features).
    """
    base = Config()
    for gw, gh in [(16, 12), (32, 24)]:
        for indirect_thresh in [0.0, 95.0, 105.0, 115.0]:
            for tile_z in [2.0, 2.5, 3.0]:
                for q_ratio in [0.15, 0.20, 0.25]:
                    for d_delta in [20.0, 25.0, 30.0, 35.0]:
                        for t_delta in [10.0, 15.0, 20.0]:
                            for spot_tiles in [0, 5]:           # 0=disabled, 5=production
                                for spot_stab in [5.0, 8.0, 10.0]:  # max global delta
                                    yield replace(
                                        base,
                                        grid_w=gw, grid_h=gh,
                                        indirect_light_threshold=indirect_thresh,
                                        tile_z_threshold=tile_z,
                                        quiet_anomaly_ratio=q_ratio,
                                        dark_object_min_delta=d_delta,
                                        temporal_dark_delta=t_delta,
                                        spot_change_max_tiles=spot_tiles,
                                        spot_change_global_stability=spot_stab,
                                    )


def cfg_summary(cfg: Config) -> str:
    res = "QQVGA" if cfg.grid_w == 16 else "QVGA"
    spot = f" spot={cfg.spot_change_max_tiles}@{cfg.spot_change_global_stability}" if cfg.spot_change_max_tiles else " spot=off"
    return (f"{res} indirect={cfg.indirect_light_threshold} "
            f"z={cfg.tile_z_threshold} q={cfg.quiet_anomaly_ratio} "
            f"dδ={cfg.dark_object_min_delta} tδ={cfg.temporal_dark_delta}{spot}")


def print_row(tag: str, r: dict, cfg_str: str = "") -> None:
    print(f"  {tag:24s}  TP={r['TP']:3d} FN={r['FN']:3d} FP={r['FP']:3d} TN={r['TN']:3d}  "
          f"nc_recall={r['nc_recall']:.3f}  c_recall={r['c_recall']:.3f}  {cfg_str}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Caching tile features (QQVGA 16×12 and QVGA 32×24)...")
    t0 = time.time()
    cache_qqvga = load_cache(grid_w=16, grid_h=12)
    cache_qvga  = load_cache(grid_w=32, grid_h=24)
    caches = {(16, 12): cache_qqvga, (32, 24): cache_qvga}
    print(f"  cached {len(cache_qqvga)} frames × 2 grids in {time.time()-t0:.1f}s")

    # 1. Production baselines (both resolutions, no INDIRECT_LIGHT)
    base_qqvga = Config(grid_w=16, grid_h=12)
    base_qvga  = Config(grid_w=32, grid_h=24)
    print("\n--- BASELINE (current QQVGA, no INDIRECT_LIGHT) ---")
    base_r = evaluate(base_qqvga, cache_qqvga)
    print_row("QQVGA baseline", base_r, cfg_summary(base_qqvga))
    print(f"    triggers: {dict(sorted(base_r['triggers'].items()))}")

    print("\n--- BASELINE (QVGA resolution, no INDIRECT_LIGHT) ---")
    qvga_r = evaluate(base_qvga, cache_qvga)
    print_row("QVGA  baseline", qvga_r, cfg_summary(base_qvga))
    print(f"    triggers: {dict(sorted(qvga_r['triggers'].items()))}")

    # 2. Ablations on QQVGA baseline
    print("\n--- STAGE ABLATIONS (QQVGA baseline, one rule disabled) ---")
    for stage in ALL_STAGES:
        enabled = frozenset(s for s in ALL_STAGES if s != stage)
        r = evaluate(base_qqvga, cache_qqvga, enabled)
        delta_fn = r["FN"] - base_r["FN"]
        delta_tn = r["TN"] - base_r["TN"]
        print_row(f"NO {stage:<13s}", r, f"ΔFN={delta_fn:+d}  ΔTN={delta_tn:+d}")

    # 3. Full parameter grid
    print("\n--- PARAMETER GRID SWEEP ---")
    grid_list = list(grid())
    print(f"  evaluating {len(grid_list)} configs...")
    t0 = time.time()
    results: list[tuple[Config, dict]] = []
    for i, cfg in enumerate(grid_list):
        cache = caches[(cfg.grid_w, cfg.grid_h)]
        r = evaluate(cfg, cache)
        results.append((cfg, r))
        if (i + 1) % 500 == 0:
            pace = (i + 1) / (time.time() - t0)
            eta = (len(grid_list) - i - 1) / pace
            print(f"    {i+1}/{len(grid_list)}  ({pace:.0f}/s, ETA {eta:.0f}s)")
    print(f"  done in {time.time()-t0:.1f}s")

    fn_zero = [(c, r) for c, r in results if r["FN"] == 0]
    print(f"\n  configs with FN==0: {len(fn_zero)} / {len(results)}")

    print("\n--- TOP 15 BY FN==0 then max TN ---")
    fn_zero.sort(key=lambda cr: (-cr[1]["TN"], cr[1]["FP"]))
    for cfg, r in fn_zero[:15]:
        print_row("", r, cfg_summary(cfg))

    print("\n--- TOP 5 FN==0 QQVGA  vs  TOP 5 FN==0 QVGA ---")
    qqvga_fn0 = [(c, r) for c, r in fn_zero if c.grid_w == 16]
    qvga_fn0  = [(c, r) for c, r in fn_zero if c.grid_w == 32]
    print("  QQVGA:")
    for cfg, r in qqvga_fn0[:5]:
        print_row("   ", r, cfg_summary(cfg))
    print("  QVGA:")
    for cfg, r in qvga_fn0[:5]:
        print_row("   ", r, cfg_summary(cfg))


if __name__ == "__main__":
    main()
