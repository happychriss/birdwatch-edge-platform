"""RETIRED 2026-09-05 — kept for reference only, do not run.

This assumes the per-tile background model (EMA z-score over photo buckets,
stages WARMUP / DARK_BLOB / QUIET / AMBIGUOUS).  That model was removed from the
firmware after measurement showed it could not separate birds on this scene:
32% recall at a 10% false-positive rate, against a requirement of 100%.

Suppression now happens before the camera is powered, from the clock alone —
solar elevation, quiet gap, burst position.  See presuppress_model.py, which
fits that rule and exports the lookup table the firmware uses, and experiment.md
for how the conclusion was reached.
"""
"""validate_burst.py — Evaluate the burst-mode sequence filter.

Run from src/cloud-check/:
    python validate_burst.py [--detail] [--sweep]

Reads training data from /workspace/training-data/ and evaluates:
  - ignore-sun_shining/   (goal: suppress as many as possible)
  - process-*/            (goal: suppress NONE — any suppressed frame is a bug)

Frames within each folder are processed in chronological order:
  - ignore-sun_shining:  sorted by filename timestamp (YYYYMMDD_HHMMSS)
  - process folders:     sorted by file modification time (as user requested)
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from cloud_check.burst_filter import BurstConfig, BurstResult, burst_classify
from cloud_check.features import FRAME_W, FRAME_H, GRID_W, GRID_H, extract_tile_features_yuv

_candidates = [Path('/workspace/training-data'), Path(__file__).parents[2] / 'training-data']
TRAINING_DATA = next((p for p in _candidates if p.exists()), _candidates[0])

SUN_FOLDER    = TRAINING_DATA / 'ignore-sun_shining'
PROCESS_FOLDERS = [
    TRAINING_DATA / 'process-birds-pillow',
    TRAINING_DATA / 'process-people',
    TRAINING_DATA / 'process-dark',
    TRAINING_DATA / 'process-real-birds',
]


# ── helpers ───────────────────────────────────────────────────────────────────

def load_tile_means(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Return (tile_mean_y, tile_mean_u, tile_mean_v, global_mean).

    All tile arrays are (GRID_H×GRID_W float32). BT.601 YCbCr, U/V centered at 128.
    """
    with Image.open(path) as im:
        ycbcr = im.convert('YCbCr').resize((FRAME_W, FRAME_H), Image.Resampling.BILINEAR)
        arr = np.asarray(ycbcr, dtype=np.uint8)
    feats = extract_tile_features_yuv(arr[:, :, 0], arr[:, :, 1], arr[:, :, 2])
    return feats['mean_y'], feats['mean_u'], feats['mean_v'], float(feats['global_mean'])


@dataclass
class EvalRow:
    filename: str
    source: str
    true_label: str        # "sun" | "process"
    dt_seconds: float
    gm: float
    gm_diff: float
    result: BurstResult

    @property
    def correct(self) -> bool:
        if self.true_label == 'sun':
            return self.result.label == 'suppress'
        else:
            return self.result.label == 'process'


def _ts_from_name(name: str) -> datetime:
    try:
        return datetime.strptime(name[:15], '%Y%m%d_%H%M%S')
    except ValueError:
        return datetime.min


def run_folder(
    folder: Path,
    true_label: str,
    sort_by_mtime: bool,
    cfg: BurstConfig,
    verbose: bool = False,
) -> list[EvalRow]:
    """Process all JPEGs in folder in sequence, return per-frame EvalRow list."""
    files = [f for f in folder.iterdir() if f.suffix.lower() == '.jpg']
    if not files:
        return []

    if sort_by_mtime:
        files.sort(key=lambda p: p.stat().st_mtime)
    else:
        files.sort(key=lambda p: p.name)

    rows: list[EvalRow] = []
    prev_tile_mean_y: np.ndarray | None = None
    prev_tile_mean_u: np.ndarray | None = None
    prev_tile_mean_v: np.ndarray | None = None
    prev_gm: float | None = None
    prev_ts: datetime | None = None

    for path in files:
        tile_mean_y, tile_mean_u, tile_mean_v, gm = load_tile_means(path)
        ts = _ts_from_name(path.name)

        if prev_ts is None or prev_tile_mean_y is None:
            dt = float('inf')
        else:
            dt = (ts - prev_ts).total_seconds()
            if sort_by_mtime:
                # When sorting by mtime the filename timestamp is the authoritative
                # capture time; a negative dt means two separate sessions that happen
                # to be mtime-ordered opposite to capture order.  Treat as isolated.
                if dt < 0:
                    dt = float('inf')

        result = burst_classify(
            tile_mean_y, gm, prev_tile_mean_y, prev_gm, dt, cfg,
            tile_mean_u=tile_mean_u,
            tile_mean_v=tile_mean_v,
            prev_tile_mean_u=prev_tile_mean_u,
            prev_tile_mean_v=prev_tile_mean_v,
        )
        gm_diff = abs(gm - prev_gm) if prev_gm is not None else 0.0

        rows.append(EvalRow(
            filename=path.name,
            source=folder.name,
            true_label=true_label,
            dt_seconds=dt,
            gm=gm,
            gm_diff=gm_diff,
            result=result,
        ))

        if verbose:
            mark = '✓' if rows[-1].correct else '✗'
            print(f"  {mark} {path.name:35s}  dt={dt:6.0f}s  gm={gm:6.1f}  "
                  f"Δgm={gm_diff:+6.1f}  n={result.n_changed:3d}  nd={result.n_dark:3d}  "
                  f"nc={result.n_chroma_changed:3d}  blob={result.blob_max:3d}  "
                  f"{result.trigger:<17s}  → {result.label}")

        # Always update prev from the most recently captured frame
        prev_tile_mean_y = tile_mean_y
        prev_tile_mean_u = tile_mean_u
        prev_tile_mean_v = tile_mean_v
        prev_gm = gm
        prev_ts = ts

    return rows


def run_process_combined(
    folders: list[Path],
    cfg: BurstConfig,
    verbose: bool = False,
) -> list[EvalRow]:
    """Load all process frames from all folders, sort by mtime globally, then evaluate."""
    all_files: list[tuple[float, Path]] = []
    for folder in folders:
        if not folder.exists():
            continue
        for p in folder.iterdir():
            if p.suffix.lower() == '.jpg':
                all_files.append((p.stat().st_mtime, p))

    all_files.sort()

    rows: list[EvalRow] = []
    prev_tile_mean_y: np.ndarray | None = None
    prev_tile_mean_u: np.ndarray | None = None
    prev_tile_mean_v: np.ndarray | None = None
    prev_gm: float | None = None
    prev_ts: datetime | None = None

    for _, path in all_files:
        tile_mean_y, tile_mean_u, tile_mean_v, gm = load_tile_means(path)
        ts = _ts_from_name(path.name)

        if prev_ts is None or prev_tile_mean_y is None:
            dt = float('inf')
        else:
            dt = (ts - prev_ts).total_seconds()
            if dt < 0:
                dt = float('inf')  # separate session ordered by mtime but reverse capture order

        result = burst_classify(
            tile_mean_y, gm, prev_tile_mean_y, prev_gm, dt, cfg,
            tile_mean_u=tile_mean_u,
            tile_mean_v=tile_mean_v,
            prev_tile_mean_u=prev_tile_mean_u,
            prev_tile_mean_v=prev_tile_mean_v,
        )
        gm_diff = abs(gm - prev_gm) if prev_gm is not None else 0.0

        rows.append(EvalRow(
            filename=path.name,
            source=path.parent.name,
            true_label='process',
            dt_seconds=dt,
            gm=gm,
            gm_diff=gm_diff,
            result=result,
        ))

        if verbose:
            mark = '✓' if rows[-1].correct else '✗'
            print(f"  {mark} {path.name:35s} [{path.parent.name:25s}]  "
                  f"dt={dt:7.0f}s  gm={gm:6.1f}  Δgm={gm_diff:+6.1f}  "
                  f"n={result.n_changed:3d}  nd={result.n_dark:3d}  nc={result.n_chroma_changed:3d}  "
                  f"blob={result.blob_max:3d}  {result.trigger:<17s}  → {result.label}")

        prev_tile_mean_y = tile_mean_y
        prev_tile_mean_u = tile_mean_u
        prev_tile_mean_v = tile_mean_v
        prev_gm = gm
        prev_ts = ts

    return rows


def print_summary(rows: list[EvalRow], label: str) -> None:
    n = len(rows)
    suppressed = sum(1 for r in rows if r.result.label == 'suppress')
    processed  = n - suppressed
    correct    = sum(1 for r in rows if r.correct)
    errors     = [r for r in rows if not r.correct]

    trigger_counts: dict[str, int] = {}
    for r in rows:
        trigger_counts[r.result.trigger] = trigger_counts.get(r.result.trigger, 0) + 1

    print(f"\n{'─'*60}")
    print(f"  {label}")
    print(f"{'─'*60}")
    print(f"  Total frames : {n}")
    print(f"  Suppressed   : {suppressed}  ({100*suppressed/n:.1f}%)")
    print(f"  Processed    : {processed}  ({100*processed/n:.1f}%)")
    print(f"  Correct      : {correct}  ({100*correct/n:.1f}%)")
    print()
    print("  Trigger breakdown:")
    for trigger, count in sorted(trigger_counts.items(), key=lambda x: -x[1]):
        label_for_trigger = 'suppress' if trigger in ('DUPLICATE', 'DIFFUSE', 'BRIGHT_STABLE', 'FAST_SHIFT') else 'process'
        print(f"    {trigger:<18s}  {count:4d}  → {label_for_trigger}")

    if errors:
        print(f"\n  ERRORS ({len(errors)}):")
        for r in errors:
            print(f"    ✗ {r.filename:35s} [{r.source}]  "
                  f"dt={r.dt_seconds:6.0f}s  Δgm={r.gm_diff:+6.1f}  "
                  f"n={r.result.n_changed:3d}  nd={r.result.n_dark:3d}  blob={r.result.blob_max:3d}  "
                  f"{r.result.trigger}  → {r.result.label}  (expected: {r.true_label})")


def sweep(sun_rows_fn, proc_rows_fn) -> None:
    """Grid search over key thresholds, print best configs with 0 process errors."""
    import itertools

    best: list[tuple[float, BurstConfig, int, int]] = []

    burst_windows   = [60.0, 120.0, 180.0]
    brightness_thrs = [10.0, 15.0, 20.0]
    tile_diff_thrs  = [8.0, 10.0, 12.0]
    diffuse_mins    = [50, 60, 70, 80, 90, 100, 120, 150]

    # "suppress_birds_ok=False" means only check bird/pillow folders for errors;
    # people folders are allowed to have suppressed frames (user confirmed acceptable).
    total = len(burst_windows) * len(brightness_thrs) * len(tile_diff_thrs) * len(diffuse_mins)
    print(f"\nSweeping {total} configurations...")
    print("(People suppressed = acceptable; only bird/pillow frames counted as errors)")

    for bw, bt, td, dm in itertools.product(
        burst_windows, brightness_thrs, tile_diff_thrs, diffuse_mins
    ):
        cfg = BurstConfig(
            burst_window_seconds=bw,
            brightness_sim_threshold=bt,
            tile_diff_threshold=td,
            dark_diff_threshold=td,
            duplicate_max_tiles=0,
            diffuse_min_dark_tiles=dm,
        )
        s_rows = sun_rows_fn(cfg)
        p_rows = proc_rows_fn(cfg)

        # Only count bird/pillow suppressions as errors
        bird_errors = sum(1 for r in p_rows
                          if not r.correct and 'bird' in r.source.lower())
        sun_suppressed = sum(1 for r in s_rows if r.result.label == 'suppress')
        people_suppressed = sum(1 for r in p_rows
                                if r.result.label == 'suppress' and 'people' in r.source.lower())

        if bird_errors == 0:
            best.append((sun_suppressed / len(s_rows), cfg, sun_suppressed,
                         len(s_rows), people_suppressed))

    best.sort(key=lambda x: -x[0])
    print(f"\nTop 10 configs with 0 bird errors (sorted by sun suppression rate):")
    print(f"  {'sun_sup%':>8}  {'ppl_sup':>7}  {'bw':>5}  {'bt':>5}  {'td':>5}  {'dm':>5}")
    for rate, cfg, sup, tot, psup in best[:10]:
        print(f"  {100*rate:>7.1f}%  {psup:>7d}  {cfg.burst_window_seconds:>5.0f}  "
              f"{cfg.brightness_sim_threshold:>5.1f}  {cfg.tile_diff_threshold:>5.1f}  "
              f"{cfg.diffuse_min_dark_tiles:>5d}")

    if best:
        best_cfg = best[0][1]
        print(f"\nBest config: {best_cfg}")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = set(sys.argv[1:])
    verbose = '--detail' in args
    do_sweep = '--sweep' in args

    cfg = BurstConfig()
    print(f"BurstConfig: {cfg}")

    # ── Sun-shining evaluation ────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("SUN-SHINING folder (sorted by filename timestamp)")
    print(f"{'='*60}")
    if verbose:
        print()
    sun_rows = run_folder(SUN_FOLDER, 'sun', sort_by_mtime=False, cfg=cfg, verbose=verbose)
    print_summary(sun_rows, f"ignore-sun_shining  ({len(sun_rows)} frames)")

    # ── Process evaluation ────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("PROCESS folders (combined, sorted by mtime)")
    print(f"{'='*60}")
    if verbose:
        print()
    proc_rows = run_process_combined(PROCESS_FOLDERS, cfg, verbose=verbose)
    print_summary(proc_rows, f"process-*  ({len(proc_rows)} frames)")

    # ── Per-process-folder breakdown ──────────────────────────────────────────
    sources = sorted({r.source for r in proc_rows})
    for src in sources:
        sub = [r for r in proc_rows if r.source == src]
        suppressed = sum(1 for r in sub if r.result.label == 'suppress')
        print(f"  {src:<30s}  {len(sub):3d} frames  suppressed={suppressed}")

    # ── Sweep ────────────────────────────────────────────────────────────────
    if do_sweep:
        sweep(
            lambda cfg: run_folder(SUN_FOLDER, 'sun', False, cfg),
            lambda cfg: run_process_combined(PROCESS_FOLDERS, cfg),
        )


if __name__ == '__main__':
    main()
