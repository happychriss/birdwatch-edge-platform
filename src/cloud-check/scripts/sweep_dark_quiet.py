"""Sweep comparing standard QUIET (absolute z-score ratio) vs dark-only QUIET
(only tiles darker than model count toward anomaly ratio).

Runs across three grid resolutions: 16x12, 20x15, 32x24.

Usage (from /workspace/src/cloud-check):
    .venv/bin/python -m scripts.sweep_dark_quiet
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, replace, field
from pathlib import Path
from typing import Iterable

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_check.background import BackgroundModel
from cloud_check.config import Config
from cloud_check.dataset import load_dataset
from cloud_check.features import extract_tile_features, load_gray_vga

ALL_STAGES = ("NIGHT", "WARMUP", "DARK_OBJ", "INDIRECT_LIGHT", "SPOT_CHANGE", "QUIET", "SCENE_DRIFT", "AMBIGUOUS")


@dataclass
class CachedSample:
    label: str
    hour: int
    tile_mean: np.ndarray
    filename: str


def load_cache(grid_w: int, grid_h: int) -> list[CachedSample]:
    raw = [s for s in load_dataset() if s.domain != "aux-2025"]
    raw.sort(key=lambda s: (s.taken_at.isoformat() if s.taken_at else s.path.name))
    cache = []
    for s in raw:
        frame = load_gray_vga(s.path)
        feats = extract_tile_features(frame, grid_w=grid_w, grid_h=grid_h)
        cache.append(CachedSample(
            label=s.label, hour=s.hour_bucket,
            tile_mean=feats["mean"], filename=s.path.name,
        ))
    return cache


def classify_inline(
    tile_mean: np.ndarray,
    hour: int,
    model: BackgroundModel,
    cfg: Config,
    prev_tile_mean: np.ndarray | None,
    quiet_dark_only: bool,
) -> tuple[str, str]:
    if cfg.night_brightness_threshold > 0 and tile_mean.mean() < cfg.night_brightness_threshold:
        return "process", "NIGHT"

    bucket = model._idx(hour)
    z = np.abs(tile_mean - model.mean[bucket]) / np.sqrt(model.var[bucket])
    mask = z > cfg.tile_z_threshold

    if quiet_dark_only:
        # Only tiles darker than the model count toward anomaly ratio.
        dark_mask = mask & (tile_mean < model.mean[bucket])
        ratio = float(dark_mask.mean())
    else:
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
        dark_tiles >= cfg.dark_object_min_tiles
        and (not temporal_available or new_dark_tiles >= cfg.dark_object_min_tiles)
    )
    stale_fires = (
        dark_tiles >= cfg.scene_drift_min_tiles
        and temporal_available
        and new_dark_tiles < cfg.dark_object_min_tiles
    )

    if warmup:
        return "process", "WARMUP"
    if dark_obj_fires:
        return "process", "DARK_OBJ"
    if cfg.indirect_light_threshold > 0 and float(tile_mean.mean()) < cfg.indirect_light_threshold:
        return "process", "INDIRECT_LIGHT"
    if (
        prev_tile_mean is not None
        and cfg.spot_change_max_tiles > 0
        and abs(float(tile_mean.mean()) - float(prev_tile_mean.mean())) < cfg.spot_change_global_stability
        and 1 <= int((tile_mean - prev_tile_mean < -cfg.spot_change_tile_delta).sum()) <= cfg.spot_change_max_tiles
        and int((np.abs(tile_mean - prev_tile_mean) >= 10).sum()) <= cfg.spot_change_max_noisy_tiles
    ):
        return "process", "SPOT_CHANGE"
    if ratio <= cfg.quiet_anomaly_ratio:
        return "clouds", "QUIET"
    if stale_fires:
        return "process", "SCENE_DRIFT"
    return "process", "AMBIGUOUS"


def evaluate(cfg: Config, cache: list[CachedSample], quiet_dark_only: bool) -> dict:
    model = BackgroundModel(cfg)
    prev_mean: dict[int, np.ndarray] = {}
    tp = fn = fp = tn = 0
    trigger_counts: dict[str, int] = {}

    for s in cache:
        bucket = model._idx(s.hour)
        prev = prev_mean.get(bucket)
        was_warmup = model.warmup_remaining(s.hour) > 0
        model.observe(s.hour)
        label, trigger = classify_inline(s.tile_mean, s.hour, model, cfg, prev, quiet_dark_only)
        trigger_counts[trigger] = trigger_counts.get(trigger, 0) + 1

        if s.label == "process" and label == "process":   tp += 1
        elif s.label == "process" and label == "clouds":  fn += 1
        elif s.label == "clouds"  and label == "process": fp += 1
        else:                                             tn += 1

        if was_warmup or label == "clouds" or trigger in ("SCENE_DRIFT", "NIGHT", "INDIRECT_LIGHT"):
            model.update(s.hour, s.tile_mean)
        if trigger == "SCENE_DRIFT":
            model.reset_warmup(s.hour)
        prev_mean[bucket] = s.tile_mean

    nc = tp / (tp + fn) if (tp + fn) else 0.0
    c  = tn / (tn + fp) if (tn + fp) else 0.0
    return {"TP": tp, "FN": fn, "FP": fp, "TN": tn,
            "nc_recall": nc, "c_recall": c, "triggers": trigger_counts}


def sweep(grid_w: int, grid_h: int) -> Iterable[tuple[Config, bool]]:
    base = Config()
    for dark_only in [False, True]:
        for indirect in [0.0, 95.0, 105.0, 115.0]:
            for tile_z in [2.0, 2.5, 3.0]:
                for q_ratio in [0.10, 0.15, 0.20, 0.25, 0.30]:
                    for d_delta in [20.0, 25.0, 30.0, 35.0]:
                        for t_delta in [10.0, 15.0, 20.0]:
                            for spot_tiles in [0, 2]:
                                for spot_stab in [5.0, 10.0]:
                                    yield replace(
                                        base,
                                        grid_w=grid_w, grid_h=grid_h,
                                        indirect_light_threshold=indirect,
                                        tile_z_threshold=tile_z,
                                        quiet_anomaly_ratio=q_ratio,
                                        dark_object_min_delta=d_delta,
                                        temporal_dark_delta=t_delta,
                                        spot_change_max_tiles=spot_tiles,
                                        spot_change_global_stability=spot_stab,
                                    ), dark_only


def fmt(tag: str, r: dict, extra: str = "") -> str:
    return (f"  {tag:<32s}  TP={r['TP']:3d} FN={r['FN']:3d} FP={r['FP']:3d} TN={r['TN']:3d}"
            f"  nc={r['nc_recall']:.3f}  c={r['c_recall']:.3f}  {extra}")


def run_grid(label: str, grid_w: int, grid_h: int, cache: list[CachedSample]) -> dict:
    base = Config(grid_w=grid_w, grid_h=grid_h)
    r_std  = evaluate(base, cache, quiet_dark_only=False)
    r_dark = evaluate(base, cache, quiet_dark_only=True)
    print(f"\n{'='*90}")
    print(f"  {label}")
    print(f"{'='*90}")
    print(fmt("baseline (abs ratio)", r_std))
    print(f"    triggers: {dict(sorted(r_std['triggers'].items()))}")
    print(fmt("baseline (dark-only ratio)", r_dark))
    print(f"    triggers: {dict(sorted(r_dark['triggers'].items()))}")

    configs = list(sweep(grid_w, grid_h))
    print(f"\n  Sweeping {len(configs)} configs...")
    t0 = time.time()
    results = []
    for i, (cfg, dark_only) in enumerate(configs):
        r = evaluate(cfg, cache, dark_only)
        results.append((cfg, dark_only, r))
        if (i + 1) % 2000 == 0:
            pace = (i + 1) / (time.time() - t0)
            print(f"    {i+1}/{len(configs)}  ({pace:.0f}/s, ETA {(len(configs)-i-1)/pace:.0f}s)")
    print(f"  done in {time.time()-t0:.1f}s")

    fn0 = [(cfg, do, r) for cfg, do, r in results if r["FN"] == 0]
    fn0.sort(key=lambda x: (-x[2]["TN"], x[2]["FP"]))

    print(f"\n  FN==0: {len(fn0)}/{len(configs)}  "
          f"(abs={sum(1 for _,do,_ in fn0 if not do)}  dark-only={sum(1 for _,do,_ in fn0 if do)})")

    print("\n  Top 10 FN==0 (abs ratio):")
    shown = 0
    for cfg, do, r in fn0:
        if not do:
            q = f"z={cfg.tile_z_threshold} q={cfg.quiet_anomaly_ratio} dδ={cfg.dark_object_min_delta} tδ={cfg.temporal_dark_delta} indirect={cfg.indirect_light_threshold} spot={cfg.spot_change_max_tiles}"
            print(fmt("", r, q))
            shown += 1
            if shown == 10: break

    print("\n  Top 10 FN==0 (dark-only ratio):")
    shown = 0
    for cfg, do, r in fn0:
        if do:
            q = f"z={cfg.tile_z_threshold} q={cfg.quiet_anomaly_ratio} dδ={cfg.dark_object_min_delta} tδ={cfg.temporal_dark_delta} indirect={cfg.indirect_light_threshold} spot={cfg.spot_change_max_tiles}"
            print(fmt("", r, q))
            shown += 1
            if shown == 10: break

    best_abs  = next(((cfg, r) for cfg, do, r in fn0 if not do), None)
    best_dark = next(((cfg, r) for cfg, do, r in fn0 if     do), None)
    return {"label": label, "baseline_abs": r_std, "baseline_dark": r_dark,
            "best_abs": best_abs, "best_dark": best_dark}


def main() -> None:
    grids = [
        ("16x12  (10x10 px, current)", 16, 12),
        ("20x15  ( 8x8 px, proposed)", 20, 15),
        ("32x24  ( 5x5 px)",           32, 24),
    ]

    print("Caching frames...")
    t0 = time.time()
    caches = {}
    for label, gw, gh in grids:
        caches[(gw, gh)] = load_cache(gw, gh)
    print(f"  {len(caches[(16,12)])} frames x3 grids in {time.time()-t0:.1f}s")

    summaries = []
    for label, gw, gh in grids:
        s = run_grid(label, gw, gh, caches[(gw, gh)])
        summaries.append(s)

    print(f"\n\n{'='*90}")
    print("  FINAL COMPARISON  — best FN==0 per grid × mode")
    print(f"{'='*90}")
    print(f"  {'Grid + mode':<38}  TP   FN   FP   TN   nc     c")
    for s in summaries:
        lbl = s["label"]
        for tag, key in [("abs ", "best_abs"), ("dark", "best_dark")]:
            if s[key]:
                cfg, r = s[key]
                print(fmt(f"{lbl} [{tag}]", r))
            else:
                print(f"  {lbl} [{tag}]  — no FN==0 found")

    print(f"\n  DELTA: dark-only vs abs ratio (best configs, TN gain)")
    for s in summaries:
        if s["best_abs"] and s["best_dark"]:
            tn_abs  = s["best_abs"][1]["TN"]
            tn_dark = s["best_dark"][1]["TN"]
            print(f"  {s['label']:<34}  TN abs={tn_abs}  TN dark={tn_dark}  Δ={tn_dark-tn_abs:+d}")


if __name__ == "__main__":
    main()
