"""Parameter sweep for 20×15 tile grid (8×8 px tiles on QQVGA).

Compares three grids side-by-side:
  - 16×12  (current, 10×10 px tiles)
  - 20×15  (proposed, 8×8 px tiles)
  - 32×24  (existing QVGA option, 5×5 px tiles)

For each grid, finds configs with FN==0 (no missed birds/people) and ranks
by maximum TN (cloud suppression). Then runs a targeted parameter sweep over
the most impactful knobs.

Usage (from /workspace/src/cloud-check):
    .venv/bin/python -m scripts.sweep_8x8
"""

from __future__ import annotations

import itertools
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_check.config import Config
from cloud_check.dataset import load_dataset
from cloud_check.features import extract_tile_features, load_gray_vga
from scripts.sweep import CachedSample, evaluate, print_row, cfg_summary, ALL_STAGES


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


def sweep_grid(grid_w: int, grid_h: int):
    base = Config()
    for indirect_thresh in [0.0, 85.0, 95.0, 105.0, 115.0]:
        for tile_z in [1.5, 2.0, 2.5, 3.0]:
            for q_ratio in [0.10, 0.15, 0.20, 0.25]:
                for d_delta in [15.0, 20.0, 25.0, 30.0, 35.0]:
                    for t_delta in [10.0, 15.0, 20.0]:
                        for spot_tiles in [0, 2, 5]:
                            for spot_stab in [5.0, 8.0, 10.0]:
                                yield replace(
                                    base,
                                    grid_w=grid_w, grid_h=grid_h,
                                    indirect_light_threshold=indirect_thresh,
                                    tile_z_threshold=tile_z,
                                    quiet_anomaly_ratio=q_ratio,
                                    dark_object_min_delta=d_delta,
                                    temporal_dark_delta=t_delta,
                                    spot_change_max_tiles=spot_tiles,
                                    spot_change_global_stability=spot_stab,
                                )


def run_grid(label: str, grid_w: int, grid_h: int, cache: list[CachedSample]) -> None:
    base_cfg = Config(grid_w=grid_w, grid_h=grid_h)
    base_r = evaluate(base_cfg, cache)
    print(f"\n--- {label} BASELINE ---")
    print_row(f"{label} baseline", base_r, cfg_summary(base_cfg))
    print(f"    triggers: {dict(sorted(base_r['triggers'].items()))}")

    print(f"\n--- {label} STAGE ABLATIONS ---")
    for stage in ALL_STAGES:
        enabled = frozenset(s for s in ALL_STAGES if s != stage)
        r = evaluate(base_cfg, cache, enabled)
        delta_fn = r["FN"] - base_r["FN"]
        delta_tn = r["TN"] - base_r["TN"]
        print_row(f"  NO {stage:<13s}", r, f"ΔFN={delta_fn:+d}  ΔTN={delta_tn:+d}")

    configs = list(sweep_grid(grid_w, grid_h))
    print(f"\n--- {label} SWEEP ({len(configs)} configs) ---")
    t0 = time.time()
    results = []
    for i, cfg in enumerate(configs):
        r = evaluate(cfg, cache)
        results.append((cfg, r))
        if (i + 1) % 1000 == 0:
            pace = (i + 1) / (time.time() - t0)
            eta = (len(configs) - i - 1) / pace
            print(f"  {i+1}/{len(configs)}  ({pace:.0f}/s, ETA {eta:.0f}s)")
    print(f"  done in {time.time()-t0:.1f}s")

    fn_zero = [(c, r) for c, r in results if r["FN"] == 0]
    fn_zero.sort(key=lambda cr: (-cr[1]["TN"], cr[1]["FP"]))
    print(f"  FN==0 configs: {len(fn_zero)} / {len(results)}")
    print(f"  Top 10:")
    for cfg, r in fn_zero[:10]:
        print_row("   ", r, cfg_summary(cfg))
    return fn_zero


def main() -> None:
    grids = [
        ("16x12 QQVGA (current)", 16, 12),
        ("20x15 (8x8px proposed)", 20, 15),
        ("32x24 QVGA (5x5px)",    32, 24),
    ]

    print("Loading and caching frames...")
    t0 = time.time()
    caches = {}
    for label, gw, gh in grids:
        caches[(gw, gh)] = load_cache(gw, gh)
        print(f"  {label}: {len(caches[(gw,gh)])} frames cached")
    print(f"  total: {time.time()-t0:.1f}s")

    best: dict[str, list] = {}
    for label, gw, gh in grids:
        fn_zero = run_grid(label, gw, gh, caches[(gw, gh)])
        best[label] = fn_zero

    print("\n\n=== COMPARISON: best FN==0 config per grid ===")
    print(f"  {'Grid':<28}  TP   FN   FP   TN   nc_recall  c_recall  config")
    for label, gw, gh in grids:
        fn_zero = best[label]
        if fn_zero:
            cfg, r = fn_zero[0]
            print_row(label, r, cfg_summary(cfg))
        else:
            print(f"  {label:<28}  no FN==0 config found")


if __name__ == "__main__":
    main()
