"""sweep.py — Grid search over classifier parameters using stored DB tile_means.

Reads tile_means from the DB (no JPG access needed), replays the full
burst + background-model pipeline for each config, and reports recall /
false-positive rate across all labeled frames.

Usage:
    cd src/cloud-check
    source .venv/bin/activate
    python sweep.py [--top N]
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, fields
from itertools import product
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

_here = Path(__file__).parent
load_dotenv(_here.parent / 'python_bw_src' / '.env')
sys.path.insert(0, str(_here))
sys.path.insert(0, str(_here.parent / 'python_bw_src'))

from cloud_check.background import BackgroundModel
from cloud_check.burst_filter import BurstConfig, burst_classify
from cloud_check.classifier import classify
from cloud_check.config import Config
from cloud_check.scene_buckets import CENTROIDS, K as CC_K
from db import BwFrame, Session


_BURST_SUPPRESS_STAGES = frozenset({'DUPLICATE', 'BRIGHT_STABLE'})


def _load_frames() -> list[dict]:
    """Load all frames with tile_means from DB, sorted chronologically."""
    db = Session()
    try:
        rows = (db.query(BwFrame)
                .filter(BwFrame.filename.isnot(None),
                        BwFrame.meta['tile_means'].isnot(None))
                .order_by(BwFrame.captured_at.asc())
                .all())
        frames = []
        for f in rows:
            m = f.meta or {}
            tm = m.get('tile_means')
            if not tm or len(tm) != 300:
                continue
            frames.append({
                'id':          f.id,
                'captured_at': f.captured_at,
                'tile_means':  tm,
                'label':       m.get('label'),   # 'bird', 'clouds', or None
            })
        return frames
    finally:
        db.close()


def _run_sim(frames: list[dict], cfg: Config) -> dict:
    """Replay burst + BG pipeline with given config. Return per-frame results."""
    model = BackgroundModel(cfg)
    for b in range(CC_K):
        model.mean[b] = CENTROIDS[b].reshape(cfg.grid_h, cfg.grid_w).copy()
        model.bucket_seen[b] = cfg.warmup_frames_per_bucket

    burst_cfg = BurstConfig()
    prev_tile_mean = None
    prev_burst_tm   = None
    prev_burst_gm   = None
    prev_burst_ts   = None
    prev_tm_by_bucket: dict[int, np.ndarray] = {}

    results = []
    for fr in frames:
        tile_mean = np.array(fr['tile_means'], dtype=np.float32).reshape(cfg.grid_h, cfg.grid_w)
        gm = float(tile_mean.mean())
        ts = fr['captured_at']

        if prev_burst_ts is not None and ts is not None:
            burst_dt = max(0.0, (ts.replace(tzinfo=None) - prev_burst_ts.replace(tzinfo=None)).total_seconds())
        else:
            burst_dt = float('inf')

        burst = burst_classify(tile_mean, gm, prev_burst_tm, prev_burst_gm, burst_dt, burst_cfg)
        prev_burst_tm = tile_mean
        prev_burst_gm = gm
        prev_burst_ts = ts

        burst_suppresses = (burst.label == 'suppress'
                            and burst.trigger in _BURST_SUPPRESS_STAGES)

        if burst_suppresses:
            result = 'clouds'
            trigger = burst.trigger
            prev_tile_mean = tile_mean
        else:
            bucket = model.bucket_for(tile_mean)
            bg_prev = prev_tm_by_bucket.get(bucket)
            was_warmup = model.warmup_remaining(bucket) > 0
            model.observe(bucket)
            hour = ts.hour if ts else 12
            pred = classify(tile_mean, hour, model, cfg, prev_tile_mean=bg_prev)
            result = pred.label
            trigger = pred.trigger
            if was_warmup or pred.label == 'clouds' or pred.trigger in ('SCENE_DRIFT', 'NIGHT'):
                model.update(bucket, tile_mean)
            if pred.trigger == 'SCENE_DRIFT':
                model.reset_warmup(bucket)
            prev_tm_by_bucket[bucket] = tile_mean
            prev_tile_mean = tile_mean

        results.append({
            'id':      fr['id'],
            'label':   fr['label'],
            'result':  result,
            'trigger': trigger,
        })

    return results


def _score(sim_results: list[dict]) -> dict:
    birds  = [r for r in sim_results if r['label'] == 'bird']
    clouds = [r for r in sim_results if r['label'] == 'clouds']
    unlabeled = [r for r in sim_results if r['label'] is None]

    bird_detected  = sum(1 for r in birds  if r['result'] == 'process')
    cloud_fp       = sum(1 for r in clouds if r['result'] == 'process')
    process_total  = sum(1 for r in sim_results if r['result'] == 'process')

    return {
        'bird_recall':   bird_detected / len(birds)  if birds  else 0.0,
        'bird_det':      bird_detected,
        'bird_total':    len(birds),
        'cloud_fp':      cloud_fp,
        'cloud_total':   len(clouds),
        'process_total': process_total,
        'total_frames':  len(sim_results),
    }


@dataclass
class SweepPoint:
    dark_object_min_delta:    float
    temporal_dark_delta:      float
    quiet_anomaly_ratio:      float
    dark_object_min_tiles:    int
    # Whether dark_obj_condition uses OR (either dark_tiles or new_dark >= min_tiles)
    # instead of the default AND
    dark_obj_or:              bool
    bird_recall:  float = 0.0
    cloud_fp:     int   = 0
    process_total: int  = 0


def sweep(frames: list[dict], top_n: int = 20) -> None:
    grid = {
        'dark_object_min_delta':    [20.0, 25.0, 30.0, 35.0],
        'temporal_dark_delta':      [15.0, 20.0],
        'quiet_anomaly_ratio':      [0.20, 0.25, 0.30],
        'dark_object_min_tiles':    [1, 2],
        'dark_obj_or':              [False, True],
    }

    print(f"Sweeping {2**0 * len(grid['dark_object_min_delta']) * len(grid['temporal_dark_delta']) * len(grid['quiet_anomaly_ratio']) * len(grid['dark_object_min_tiles']) * len(grid['dark_obj_or'])} configs ...", flush=True)

    results_table: list[SweepPoint] = []

    for delta, t_delta, q_ratio, min_tiles, or_mode in product(
        grid['dark_object_min_delta'],
        grid['temporal_dark_delta'],
        grid['quiet_anomaly_ratio'],
        grid['dark_object_min_tiles'],
        grid['dark_obj_or'],
    ):
        cfg = Config(
            num_time_buckets=CC_K,
            warmup_frames_per_bucket=4,
            dark_object_min_delta=delta,
            temporal_dark_delta=t_delta,
            quiet_anomaly_ratio=q_ratio,
            dark_object_min_tiles=min_tiles,
        )

        sim = _run_sim(frames, cfg)

        # Optionally patch dark_obj_condition to use OR
        if or_mode:
            sim = _run_sim_or_mode(frames, cfg)

        sc = _score(sim)
        results_table.append(SweepPoint(
            dark_object_min_delta=delta,
            temporal_dark_delta=t_delta,
            quiet_anomaly_ratio=q_ratio,
            dark_object_min_tiles=min_tiles,
            dark_obj_or=or_mode,
            bird_recall=sc['bird_recall'],
            cloud_fp=sc['cloud_fp'],
            process_total=sc['process_total'],
        ))

    # Sort by recall desc, then process_total asc (fewer false positives)
    results_table.sort(key=lambda r: (-r.bird_recall, r.process_total))

    print(f"\nTop {top_n} configs (sorted by recall ↑, process_total ↓):")
    print(f"{'delta':>6} {'t_delta':>7} {'q_ratio':>7} {'min_t':>5} {'OR':>3} | "
          f"{'recall':>7} {'FP':>4} {'proc/total':>10}")
    print('-' * 70)
    for r in results_table[:top_n]:
        print(f"{r.dark_object_min_delta:>6.0f} {r.temporal_dark_delta:>7.0f} "
              f"{r.quiet_anomaly_ratio:>7.2f} {r.dark_object_min_tiles:>5} "
              f"{'Y' if r.dark_obj_or else 'N':>3} | "
              f"{r.bird_recall:>7.1%} {r.cloud_fp:>4} "
              f"{r.process_total:>4}/{results_table[0].process_total if results_table else 0:>4}")
    print()
    best = results_table[0]
    print(f"Best: delta={best.dark_object_min_delta} t_delta={best.temporal_dark_delta} "
          f"q={best.quiet_anomaly_ratio} min_tiles={best.dark_object_min_tiles} "
          f"OR={best.dark_obj_or}  → recall={best.bird_recall:.1%} "
          f"process={best.process_total}/{len(frames)}")


def _run_sim_or_mode(frames: list[dict], cfg: Config) -> list[dict]:
    """Like _run_sim but dark_obj_condition uses OR instead of AND for tile counts."""
    import cloud_check.classifier as _clf_mod
    import types

    # Monkey-patch classify for this call only
    orig_classify = _clf_mod.classify

    def _patched_classify(tile_mean, hour, model, cfg2=None, prev_tile_mean=None):
        cfg2 = cfg2 or model.cfg
        b = model.bucket_for(tile_mean)
        from cloud_check.classifier import ClassifierResult
        from scipy.ndimage import label as cc_label

        global_mean = int(tile_mean.mean())
        if cfg2.night_brightness_threshold > 0 and global_mean < cfg2.night_brightness_threshold:
            return ClassifierResult(
                label="process", trigger="NIGHT",
                anomaly_mask=np.zeros(tile_mean.shape, dtype=bool),
                blob_max_size=0, anomaly_ratio=0.0, compactness=0.0,
                reason=f"NIGHT gm={global_mean}",
                warmup=model.warmup_remaining(b) > 0,
                new_dark_tiles=0, temporal_available=prev_tile_mean is not None,
                dark_tiles=0, scene_bucket=b,
            )

        z = model.z_scores(b, tile_mean)
        bucket_mean = model.mean[b]
        z_mask = z > cfg2.tile_z_threshold
        dark_mask = tile_mean < bucket_mean
        mask = z_mask & dark_mask
        total_anom = int(mask.sum())
        ratio = float(mask.mean())
        delta = tile_mean - bucket_mean
        dark_tiles = int((delta < -cfg2.dark_object_min_delta).sum())

        if prev_tile_mean is not None:
            temporal_delta = tile_mean - prev_tile_mean
            new_dark_tiles = int((temporal_delta < -cfg2.temporal_dark_delta).sum())
            temporal_available = True
        else:
            new_dark_tiles = dark_tiles
            temporal_available = False

        labelled, _ = cc_label(mask)
        blob_max = 0 if labelled.max() == 0 else int(np.bincount(labelled.ravel())[1:].max())
        compactness = blob_max / total_anom if total_anom > 0 else 0.0
        warmup = model.warmup_remaining(b) > 0

        # OR mode: either dark_tiles OR new_dark_tiles meets min threshold
        if temporal_available:
            dark_obj_condition = (
                dark_tiles >= cfg2.dark_object_min_tiles
                or new_dark_tiles >= cfg2.dark_object_min_tiles
            )
        else:
            dark_obj_condition = dark_tiles >= cfg2.dark_object_min_tiles

        stale_condition = (
            dark_tiles >= cfg2.scene_drift_min_tiles
            and temporal_available
            and new_dark_tiles < cfg2.dark_object_min_tiles
        )

        if warmup:
            trigger, decision = "WARMUP", "process"
            reason = "warmup"
        elif dark_obj_condition:
            trigger, decision = "DARK_OBJ", "process"
            reason = f"dark_tiles={dark_tiles} new_dark={new_dark_tiles}"
        elif ratio <= cfg2.quiet_anomaly_ratio:
            trigger, decision = "QUIET", "clouds"
            reason = f"quiet ratio={ratio:.3f}"
        elif stale_condition:
            trigger, decision = "SCENE_DRIFT", "process"
            reason = "scene drift"
        else:
            trigger, decision = "AMBIGUOUS", "process"
            reason = "ambiguous"

        return ClassifierResult(
            label=decision, trigger=trigger, anomaly_mask=mask,
            blob_max_size=blob_max, anomaly_ratio=ratio, compactness=compactness,
            reason=reason, warmup=warmup, new_dark_tiles=new_dark_tiles,
            temporal_available=temporal_available, dark_tiles=dark_tiles, scene_bucket=b,
        )

    _clf_mod.classify = _patched_classify
    try:
        return _run_sim(frames, cfg)
    finally:
        _clf_mod.classify = orig_classify


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--top', type=int, default=20)
    args = ap.parse_args()

    print("Loading frames from DB ...", flush=True)
    frames = _load_frames()
    labeled = sum(1 for f in frames if f['label'] in ('bird', 'clouds'))
    birds   = sum(1 for f in frames if f['label'] == 'bird')
    print(f"  {len(frames)} frames total, {birds} bird, {labeled - birds} clouds labeled\n",
          flush=True)

    sweep(frames, top_n=args.top)
