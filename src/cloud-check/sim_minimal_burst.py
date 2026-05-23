"""sim_minimal_burst.py — Simulation with Layer-1 reduced to DUPLICATE-only.

Layer 1: suppress only pixel-identical re-fires (n_changed == 0).
         No BRIGHTNESS_SHIFT, BRIGHT_STABLE, DIFFUSE, ISOLATED, FAST_SHIFT.
Layer 2: full K=4 bucket background model (unchanged).

Outputs a summary comparing recall/precision against the standard run.
Does NOT write to the database — read-only simulation.
"""
from __future__ import annotations
import sys
from pathlib import Path
from datetime import datetime

_here = Path(__file__).parent
_server_dir = _here.parent / 'python_bw_src'
sys.path.insert(0, str(_here))
sys.path.insert(0, str(_server_dir))

from dotenv import load_dotenv
load_dotenv(_server_dir / '.env')

import numpy as np

from cloud_check.background import BackgroundModel
from cloud_check.classifier import classify
from cloud_check.config import Config
from cloud_check.scene_buckets import CENTROIDS, K as CC_K
from db import BwFrame, Session

_BURST_SUPPRESS_STAGES = frozenset({'DUPLICATE', 'BRIGHT_STABLE'})  # standard

CC_N_DARK_DELTA = 12   # burst filter tile-change threshold (DN)
CC_DUPLICATE_THRESHOLD = 0  # n_changed == 0 → duplicate


def _is_duplicate(tile_mean: np.ndarray, prev_tile_mean: np.ndarray) -> bool:
    n_changed = int((np.abs(tile_mean - prev_tile_mean) > CC_N_DARK_DELTA).sum())
    return n_changed == 0


def run_sim() -> None:
    session = Session()
    frames = (session.query(BwFrame)
              .filter(BwFrame.filename.isnot(None))
              .order_by(BwFrame.captured_at.asc())
              .all())

    cc_cfg = Config(num_time_buckets=CC_K, warmup_frames_per_bucket=4)
    bg_model = BackgroundModel(cc_cfg)
    for _b in range(CC_K):
        bg_model.mean[_b] = CENTROIDS[_b].reshape(cc_cfg.grid_h, cc_cfg.grid_w).copy()
        bg_model.bucket_seen[_b] = cc_cfg.warmup_frames_per_bucket

    prev_tile_mean = None
    prev_gm = None
    prev_ts = None
    prev_tile_mean_by_bucket: dict[int, np.ndarray] = {}

    results = []  # (id, label, old_result, new_result, old_stage, new_stage)

    for frame in frames:
        meta = frame.meta or {}
        stored_tm = meta.get('tile_means')
        if not (stored_tm and len(stored_tm) == 300):
            continue

        tile_mean = np.array(stored_tm, dtype=np.float32).reshape(cc_cfg.grid_h, cc_cfg.grid_w)
        gm = int(float(tile_mean.mean()))

        if prev_ts and frame.captured_at:
            dt = max(0.0, (frame.captured_at - prev_ts).total_seconds())
        else:
            dt = float('inf')

        # Minimal Layer-1: only DUPLICATE
        if prev_tile_mean is not None and _is_duplicate(tile_mean, prev_tile_mean):
            new_result = 'clouds'
            new_stage  = 'DUPLICATE'
        else:
            bucket = bg_model.bucket_for(tile_mean)
            bg_prev = prev_tile_mean_by_bucket.get(bucket)
            was_warmup = bg_model.warmup_remaining(bucket) > 0
            bg_model.observe(bucket)

            bg_pred = classify(tile_mean, 0, bg_model, cc_cfg, prev_tile_mean=bg_prev)
            new_result = bg_pred.label
            new_stage  = bg_pred.trigger

            if was_warmup or bg_pred.label == 'clouds' or bg_pred.trigger in ('SCENE_DRIFT', 'NIGHT'):
                bg_model.update(bucket, tile_mean)
            if bg_pred.trigger == 'SCENE_DRIFT':
                bg_model.reset_warmup(bucket)
            prev_tile_mean_by_bucket[bucket] = tile_mean

        prev_tile_mean = tile_mean
        prev_gm = float(gm)
        prev_ts = frame.captured_at

        label = meta.get('label')
        old_result = meta.get('result', '?')
        old_stage  = meta.get('stage', '?')
        results.append((frame.id, label, old_result, new_result, old_stage, new_stage))

    # --- Summary ---
    print("=== Minimal-burst simulation (DUPLICATE-only Layer 1) ===\n")

    bird_ids   = [r for r in results if r[1] == 'bird']
    ignore_ids = [r for r in results if r[1] == 'ignore']

    bird_detected_old   = sum(1 for r in bird_ids if r[2] == 'process')
    bird_detected_new   = sum(1 for r in bird_ids if r[3] == 'process')
    ignore_suppressed_old = sum(1 for r in ignore_ids if r[2] == 'clouds')
    ignore_suppressed_new = sum(1 for r in ignore_ids if r[3] == 'clouds')

    print(f"Bird recall:      {bird_detected_old}/{len(bird_ids)} standard  →  {bird_detected_new}/{len(bird_ids)} minimal-burst")
    print(f"Ignore suppressed: {ignore_suppressed_old}/{len(ignore_ids)} standard  →  {ignore_suppressed_new}/{len(ignore_ids)} minimal-burst")

    # Changed frames (labeled)
    changed = [(r[0], r[1], r[2], r[3], r[4], r[5])
               for r in results if r[1] in ('bird','ignore','special') and r[2] != r[3]]
    if changed:
        print(f"\n--- Labeled frames where result changed ---")
        print(f"{'id':>5} {'label':>8}  {'old':>8} → {'new':>8}  {'old_stage':<15} {'new_stage':<15}")
        for fid, lbl, old_r, new_r, old_s, new_s in changed:
            print(f"{fid:>5} {(lbl or '?'):>8}  {old_r:>8} → {new_r:>8}  {old_s:<15} {new_s:<15}")
    else:
        print("\nNo labeled frames changed result.")

    # Stage distribution comparison (all frames)
    from collections import Counter
    old_stages = Counter(r[4] for r in results)
    new_stages = Counter(r[5] for r in results)
    all_stages = sorted(set(list(old_stages) + list(new_stages)))
    print(f"\n--- Stage distribution (all {len(results)} frames) ---")
    print(f"{'stage':<18} {'standard':>9} {'minimal':>9} {'diff':>6}")
    for s in all_stages:
        o = old_stages.get(s, 0)
        n = new_stages.get(s, 0)
        print(f"{s:<18} {o:>9} {n:>9} {n-o:>+6}")


if __name__ == '__main__':
    run_sim()
