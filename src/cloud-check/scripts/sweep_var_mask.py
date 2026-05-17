"""Simulate option #2: variance-based tile masking for QUIET decision.

Two variants:
  - abs_var:  exclude tiles where model_std > threshold (raw)
  - rel_var:  exclude tiles where model_std / model_mean > threshold (CV)

Also simulates sudden-darkening scenario: takes real cloud frames, applies
a brightness ramp to simulate cloud-covers-sun, and shows how the model
reacts frame-by-frame under each mode.

Usage (from /workspace/src/cloud-check):
    .venv/bin/python -m scripts.sweep_var_mask
"""

from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_check.background import BackgroundModel
from cloud_check.config import Config
from cloud_check.dataset import load_dataset
from cloud_check.features import extract_tile_features, load_gray_vga

ALL_STAGES = ("NIGHT", "WARMUP", "DARK_OBJ", "INDIRECT_LIGHT", "SPOT_CHANGE", "QUIET", "SCENE_DRIFT", "AMBIGUOUS")


# ---------------------------------------------------------------------------
# Cached sample
# ---------------------------------------------------------------------------

from dataclasses import dataclass

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


# ---------------------------------------------------------------------------
# Classifier with variance mask + dark-only ratio
# ---------------------------------------------------------------------------

def classify_inline(
    tile_mean: np.ndarray,
    hour: int,
    model: BackgroundModel,
    cfg: Config,
    prev_tile_mean: np.ndarray | None,
    quiet_dark_only: bool,
    var_mask_mode: str,   # "none" | "abs" | "rel"
    var_threshold: float,
) -> tuple[str, str]:
    if cfg.night_brightness_threshold > 0 and tile_mean.mean() < cfg.night_brightness_threshold:
        return "process", "NIGHT"

    bucket = model._idx(hour)
    m   = model.mean[bucket]
    std = np.sqrt(model.var[bucket])

    # Build tile inclusion mask for QUIET ratio
    if var_mask_mode == "abs":
        include = std <= var_threshold                        # exclude high-std tiles
    elif var_mask_mode == "rel":
        cv = np.where(m > 1, std / m, std)                   # CV = std/mean; guard /0
        include = cv <= var_threshold
    else:
        include = np.ones_like(m, dtype=bool)

    z    = np.abs(tile_mean - m) / std
    mask = z > cfg.tile_z_threshold

    if quiet_dark_only:
        anomaly_mask = mask & (tile_mean < m) & include
    else:
        anomaly_mask = mask & include

    n_included = include.sum()
    ratio = float(anomaly_mask.sum() / n_included) if n_included > 0 else 0.0

    delta      = tile_mean - m
    dark_tiles = int(((delta < -cfg.dark_object_min_delta) & mask).sum())

    if prev_tile_mean is not None:
        new_dark_tiles   = int(((tile_mean - prev_tile_mean < -cfg.temporal_dark_delta) & mask).sum())
        temporal_available = True
    else:
        new_dark_tiles   = dark_tiles
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


def evaluate(cfg: Config, cache: list[CachedSample],
             quiet_dark_only: bool, var_mask_mode: str, var_threshold: float) -> dict:
    model = BackgroundModel(cfg)
    prev_mean: dict[int, np.ndarray] = {}
    tp = fn = fp = tn = 0
    triggers: dict[str, int] = {}

    for s in cache:
        bucket = model._idx(s.hour)
        prev = prev_mean.get(bucket)
        was_warmup = model.warmup_remaining(s.hour) > 0
        model.observe(s.hour)
        label, trigger = classify_inline(
            s.tile_mean, s.hour, model, cfg, prev,
            quiet_dark_only, var_mask_mode, var_threshold
        )
        triggers[trigger] = triggers.get(trigger, 0) + 1

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
    return {"TP": tp, "FN": fn, "FP": fp, "TN": tn, "nc": nc, "c": c, "triggers": triggers}


def fmt(tag: str, r: dict, extra: str = "") -> str:
    return (f"  {tag:<40s}  TP={r['TP']:3d} FN={r['FN']:3d} FP={r['FP']:3d} TN={r['TN']:3d}"
            f"  nc={r['nc']:.3f}  c={r['c']:.3f}  {extra}")


# ---------------------------------------------------------------------------
# Sudden-darkening scenario
# ---------------------------------------------------------------------------

def darkening_scenario(cache: list[CachedSample], cfg: Config) -> None:
    """Take the first 30 cloud frames, warm up the model on frames 1-10,
    then apply a sudden -40 DN brightness drop on frames 11-20 (simulating
    cloud covering sun), then restore brightness frames 21-30.
    Show per-frame model reaction under abs-ratio, dark-only, and CV-masked modes."""

    cloud_frames = [s for s in cache if s.label == "clouds"][:30]
    if len(cloud_frames) < 20:
        print("  (not enough cloud frames for darkening scenario)")
        return

    darkened = []
    for i, s in enumerate(cloud_frames):
        tm = s.tile_mean.copy().astype(np.float32)
        if 10 <= i < 20:
            tm = np.clip(tm - 40, 0, 255)   # sudden -40 DN global drop
        darkened.append(replace(s, tile_mean=tm.astype(s.tile_mean.dtype)
                                if hasattr(s.tile_mean, 'dtype') else tm))

    modes = [
        ("abs-ratio (current)",  False, "none", 0),
        ("dark-only ratio",      True,  "none", 0),
        ("dark-only + CV≤0.15",  True,  "rel",  0.15),
        ("dark-only + CV≤0.20",  True,  "rel",  0.20),
    ]

    print(f"\n  {'Frame':<6}  {'brightness':>10}  ", end="")
    for tag, *_ in modes:
        print(f"  {tag:<24}", end="")
    print()
    print(f"  {'':<6}  {'(global DN)':>10}  ", end="")
    for _ in modes:
        print(f"  {'label / QUIET ratio':>24}", end="")
    print()
    print("  " + "-" * 110)

    models = [BackgroundModel(cfg) for _ in modes]
    prevs  = [{} for _ in modes]

    for i, s in enumerate(darkened):
        phase = "DARK" if 10 <= i < 20 else "norm"
        gm = float(s.tile_mean.mean())
        print(f"  {i:<6}  {gm:>8.1f} {phase}  ", end="")

        for mi, (tag, dark_only, vm, vt) in enumerate(modes):
            mod = models[mi]
            prev = prevs[mi].get(mod._idx(s.hour))
            was_warmup = mod.warmup_remaining(s.hour) > 0
            mod.observe(s.hour)
            label, trigger = classify_inline(
                s.tile_mean, s.hour, mod, cfg, prev, dark_only, vm, vt
            )
            # Compute the ratio for display
            bucket = mod._idx(s.hour)
            m   = mod.mean[bucket]
            std = np.sqrt(mod.var[bucket])
            z   = np.abs(s.tile_mean - m) / std
            mask = z > cfg.tile_z_threshold
            if vm == "rel":
                cv = np.where(m > 1, std / m, std)
                include = cv <= vt
            else:
                include = np.ones_like(m, dtype=bool)
            if dark_only:
                anom = (mask & (s.tile_mean < m) & include).sum()
            else:
                anom = (mask & include).sum()
            n_inc = include.sum()
            ratio = anom / n_inc if n_inc > 0 else 0.0
            disp = f"{'QUIET' if label=='clouds' else trigger:<10} r={ratio:.2f}"
            print(f"  {disp:<24}", end="")

            if was_warmup or label == "clouds" or trigger in ("SCENE_DRIFT","NIGHT","INDIRECT_LIGHT"):
                mod.update(s.hour, s.tile_mean)
            if trigger == "SCENE_DRIFT":
                mod.reset_warmup(s.hour)
            prevs[mi][mod._idx(s.hour)] = s.tile_mean

        print()


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def main() -> None:
    GW, GH = 20, 15   # 8x8 px tiles — decided grid
    cfg = Config(grid_w=GW, grid_h=GH,
                 tile_z_threshold=3.0,
                 quiet_anomaly_ratio=0.25,
                 indirect_light_threshold=95.0,
                 spot_change_max_tiles=0)

    print("Caching frames (20x15 grid)...")
    cache = load_cache(GW, GH)
    print(f"  {len(cache)} frames")

    # --- Baselines ---
    print(f"\n{'='*90}")
    print("  BASELINES (20x15, optimised params)")
    print(f"{'='*90}")
    configs = [
        ("abs-ratio, no mask (current logic)",  False, "none", 0),
        ("dark-only, no mask",                  True,  "none", 0),
        ("dark-only + abs std≤20",              True,  "abs",  20.0),
        ("dark-only + abs std≤25",              True,  "abs",  25.0),
        ("dark-only + abs std≤30",              True,  "abs",  30.0),
        ("dark-only + CV≤0.10",                 True,  "rel",  0.10),
        ("dark-only + CV≤0.15",                 True,  "rel",  0.15),
        ("dark-only + CV≤0.20",                 True,  "rel",  0.20),
        ("dark-only + CV≤0.25",                 True,  "rel",  0.25),
    ]
    for tag, do, vm, vt in configs:
        r = evaluate(cfg, cache, do, vm, vt)
        print(fmt(tag, r))

    # --- Sweep over CV thresholds ---
    print(f"\n{'='*90}")
    print("  SWEEP: dark-only + CV mask threshold (20x15)")
    print(f"{'='*90}")
    best_abs = best_dark = None
    results = []
    for cv_t in np.arange(0.05, 0.55, 0.05):
        for q in [0.20, 0.25, 0.30]:
            for z in [2.5, 3.0]:
                c = replace(cfg, quiet_anomaly_ratio=q, tile_z_threshold=z)
                r = evaluate(c, cache, True, "rel", float(cv_t))
                results.append((cv_t, q, z, r))

    fn0 = [(cv_t, q, z, r) for cv_t, q, z, r in results if r["FN"] == 0]
    fn0.sort(key=lambda x: (-x[3]["TN"],))
    print(f"  FN==0 configs: {len(fn0)}/{len(results)}")
    print(f"  Top 15:")
    for cv_t, q, z, r in fn0[:15]:
        print(fmt("", r, f"CV≤{cv_t:.2f} q={q} z={z}"))

    # --- Sudden darkening scenario ---
    print(f"\n{'='*90}")
    print("  SUDDEN-DARKENING SCENARIO (-40 DN on frames 10-19)")
    print(f"{'='*90}")
    darkening_scenario(cache, cfg)


if __name__ == "__main__":
    main()
