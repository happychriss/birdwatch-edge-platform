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
"""backfill_hp.py — Full simplified HP pipeline backfill + evaluation.

Implements the redesigned pipeline end-to-end, using data already in the DB
(no JPG decode needed).

Layer-1 — two rules only:
    NIGHT       gm < 70                                   → process, skip L2
    DUPLICATE   burst_n_changed==0 AND burst_n_chroma==0   → clouds
    (all else)  FIRST/ISOLATED/BRIGHTNESS_SHIFT/FAST_SHIFT/
                BRIGHT_STABLE/DIFFUSE/SAFE                 → Layer-2

    Removed: BRIGHTNESS_SHIFT/FAST_SHIFT/BRIGHT_STABLE/DIFFUSE as suppressors.
    These were all luma cloud-heuristics that HP Layer-2 handles correctly.

Layer-2 — single-frame HP detection (no blob size cap):
    hp_delta = HF(model) - HF(tile)   where HF(X) = X - box_blur(X, k)
               positive = tile locally darker than model (illumination-removed)

    Channel A  dark-HP:    any tile hp_delta >= hp_threshold           → process
    Channel B  chroma:     any tile (tile_delta_chroma >= 8)
                           AND hp_delta >= chroma_hp_floor              → process
    default                                                            → clouds QUIET_HP

Stores:
    result, stage           — new HP pipeline decision
    result_prev, stage_prev — original values preserved for comparison
    model_tile_means        — HP-adjusted so overlay Δm = hp_delta
    tile_delta_luma         — hp_delta flattened (tooltip / text)
    hp_score                — max hp_delta across frame
    hp_tile_count           — tiles >= hp_threshold
    hp_chroma_tiles         — tiles passing chroma+dark gate
    hp_blur_k, hp_threshold — params used
    simulated_hp            — True marker
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

_here = Path(__file__).parent
sys.path.insert(0, str(_here))
_server_dir = _here.parent / 'python_bw_src'
sys.path.insert(0, str(_server_dir))

from dotenv import load_dotenv
load_dotenv(_server_dir / '.env')

import numpy as np
from scipy.ndimage import uniform_filter
from sqlalchemy.orm.attributes import flag_modified

from db import BwFrame, Session

GRID_H, GRID_W = 15, 20

# Layer-1 stages that suppress in the OLD pipeline; now passed to Layer-2 instead.
_OLD_SUPPRESS_NOW_PASS = {'BRIGHTNESS_SHIFT', 'FAST_SHIFT', 'BRIGHT_STABLE', 'DIFFUSE'}


def box_blur(arr: np.ndarray, k: int) -> np.ndarray:
    return uniform_filter(arr.astype(float), size=k, mode='nearest')


def compute_hp_delta(tile: np.ndarray, model: np.ndarray, k: int) -> np.ndarray:
    """HF(model) - HF(tile).  Positive = tile locally darker than model."""
    return (model - box_blur(model, k)) - (tile - box_blur(tile, k))


def hp_adjusted_model(tile: np.ndarray, model: np.ndarray, k: int) -> np.ndarray:
    """HP-adjusted model so that (model_adj - tile) == hp_delta.
    Store as model_tile_means so the overlay Δm displays hp_delta directly."""
    M_hi = model - box_blur(model, k)
    T_lo = box_blur(tile, k)
    return M_hi + T_lo


def run(
    dry_run: bool = False,
    from_frame: int | None = None,
    hp_threshold: float = 25.0,
    chroma_threshold: float = 8.0,
    chroma_hp_floor: float = 10.0,
    blur_k: int = 5,
) -> None:
    session = Session()

    q = (session.query(BwFrame)
         .filter(BwFrame.filename.isnot(None))
         .order_by(BwFrame.id))
    if from_frame is not None:
        q = q.filter(BwFrame.id >= from_frame)
    frames = q.all()

    print(f"Frames: {len(frames)}  hp_threshold={hp_threshold} DN  "
          f"chroma_threshold={chroma_threshold} DN  blur_k={blur_k}")
    print(f"Params: chroma_hp_floor={chroma_hp_floor} DN\n")

    updated = skipped = 0
    stats: dict[str, list] = defaultdict(list)   # label → list of (old_result, new_result)

    for frame in frames:
        meta = frame.meta or {}

        tm_flat  = meta.get('tile_means')
        mdl_flat = meta.get('model_tile_means')
        if not tm_flat or not mdl_flat:
            skipped += 1
            continue

        T   = np.array(tm_flat,  dtype=float).reshape(GRID_H, GRID_W)
        M   = np.array(mdl_flat, dtype=float).reshape(GRID_H, GRID_W)
        gm  = int(meta.get('global_mean', T.mean()))
        burst = meta.get('burst_trigger', 'FIRST')
        old_result = meta.get('result', frame.result or 'process')
        old_stage  = meta.get('stage', '?')
        label      = meta.get('label', '')

        # ── Layer-1 ───────────────────────────────────────────────────────────
        new_stage = new_result = None

        if gm < 70:
            new_stage  = 'NIGHT'
            new_result = 'process'

        elif burst == 'DUPLICATE':
            new_stage  = 'DUPLICATE'
            new_result = 'clouds'

        # All other burst stages (FIRST, ISOLATED, BRIGHTNESS_SHIFT, FAST_SHIFT,
        # BRIGHT_STABLE, DIFFUSE, SAFE, NIGHT-process) → Layer-2
        else:
            # ── Layer-2 HP ────────────────────────────────────────────────────
            hpd = compute_hp_delta(T, M, blur_k)
            hp_score      = float(hpd.max())
            hp_tile_count = int((hpd >= hp_threshold).sum())

            # Chroma channel — use stored tile_delta_chroma (already median-normalised)
            hp_chroma_tiles = 0
            tdc_flat = meta.get('tile_delta_chroma')
            if tdc_flat:
                tdc = np.array(tdc_flat, dtype=float).reshape(GRID_H, GRID_W)
                chroma_dark_mask = (tdc >= chroma_threshold) & (hpd >= chroma_hp_floor)
                hp_chroma_tiles = int(chroma_dark_mask.sum())

            # Decision: process if ANY channel fires
            if hp_tile_count >= 1 or hp_chroma_tiles >= 1:
                new_stage  = 'DARK_BLOB_HP'
                new_result = 'process'
            else:
                new_stage  = 'QUIET_HP'
                new_result = 'clouds'

        # ── Logging ───────────────────────────────────────────────────────────
        ts  = frame.captured_at.strftime('%Y-%m-%d %H:%M') if frame.captured_at else '?'
        src = meta.get('source', '?')
        changed = '  <CHANGED>' if new_result != old_result else ''
        lbl_tag = f'  label={label}' if label else ''
        extra = ''
        if new_stage not in ('DUPLICATE', 'NIGHT'):
            hpd_val = compute_hp_delta(T, M, blur_k)
            extra = f'  hp_score={hpd_val.max():.1f}'
        print(f"  {'DRY' if dry_run else 'UPD'} {frame.filename}  {ts} [{src}]"
              f"  burst={burst:<17} {old_stage}→{new_stage:<12} {old_result}→{new_result}"
              f"{extra}{lbl_tag}{changed}")

        # Track stats
        stats[label or '_none'].append((old_result, new_result))

        if not dry_run:
            hpd      = compute_hp_delta(T, M, blur_k)
            model_adj = hp_adjusted_model(T, M, blur_k)
            hp_sc    = float(hpd.max())
            hp_tc    = int((hpd >= hp_threshold).sum())
            tdc_flat2 = meta.get('tile_delta_chroma')
            hp_ct    = 0
            if tdc_flat2:
                tdc2 = np.array(tdc_flat2, dtype=float).reshape(GRID_H, GRID_W)
                hp_ct = int(((tdc2 >= chroma_threshold) & (hpd >= chroma_hp_floor)).sum())

            new_fields = {
                'result':           new_result,
                'stage':            new_stage,
                'result_prev':      old_result,
                'stage_prev':       old_stage,
                'model_tile_means': model_adj.flatten().round(1).tolist(),
                'tile_delta_luma':  hpd.flatten().round(1).tolist(),
                'hp_score':         round(hp_sc, 1),
                'hp_tile_count':    hp_tc,
                'hp_chroma_tiles':  hp_ct,
                'hp_blur_k':        blur_k,
                'hp_threshold':     hp_threshold,
                'simulated_hp':     True,
            }
            frame.meta   = {**meta, **new_fields}
            frame.result = new_result
            flag_modified(frame, 'meta')
            updated += 1

    if not dry_run and updated > 0:
        session.commit()

    # ── Evaluation report ─────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"{'DRY RUN — ' if dry_run else ''}Results  "
          f"(hp_threshold={hp_threshold}, chroma_threshold={chroma_threshold})")
    print(f"{'=' * 60}")
    print(f"  Updated: {updated}   Skipped (no tile/model data): {skipped}")

    def pct(n, d): return f"{n}/{d} ({100*n/d:.0f}%)" if d else "0/0"

    # Bird recall
    bird = stats.get('bird', [])
    if bird:
        hits   = sum(1 for _, nr in bird if nr == 'process')
        misses = [(old, new) for old, new in bird if new == 'clouds']
        print(f"\n  Bird recall:        {pct(hits, len(bird))}")
        if misses:
            print(f"  MISSES: {misses}")
        else:
            print(f"  No missed birds.")

    # Suppression on PIR frames that have no bird/ignore label
    def suppression_for(lbl_key: str) -> None:
        rows = stats.get(lbl_key, [])
        if not rows: return
        sup = sum(1 for _, nr in rows if nr == 'clouds')
        print(f"  {lbl_key or '(unlabelled)':<12} suppression: {pct(sup, len(rows))}")

    print()
    # Show how old-suppress stages are now handled
    print("  Old-suppress stages now passing to Layer-2:")
    old_suppressed = {k: v for k, v in stats.items()
                      for old, new in v if old == 'clouds' and new == 'process'}
    # Actually track stage transitions directly
    # Re-do with stage tracking
    print()
    for lbl in ['_none', 'ignore']:
        suppression_for(lbl)

    session.close()


def main() -> None:
    ap = argparse.ArgumentParser(description='HP pipeline full backfill + evaluation')
    ap.add_argument('--dry-run',          action='store_true')
    ap.add_argument('--from-frame',       type=int,   metavar='ID')
    ap.add_argument('--hp-threshold',     type=float, default=25.0,
                    help='HP dark anomaly threshold DN (default 25)')
    ap.add_argument('--chroma-threshold', type=float, default=8.0,
                    help='Chroma anomaly threshold DN (default 8 = sqrt(64))')
    ap.add_argument('--chroma-hp-floor',  type=float, default=10.0,
                    help='Loose HP darkness required for chroma gate (default 10 DN)')
    ap.add_argument('--blur-k',           type=int,   default=5,
                    help='Box-blur kernel size in tiles (default 5)')
    args = ap.parse_args()
    run(dry_run=args.dry_run,
        from_frame=args.from_frame,
        hp_threshold=args.hp_threshold,
        chroma_threshold=args.chroma_threshold,
        chroma_hp_floor=args.chroma_hp_floor,
        blur_k=args.blur_k)


if __name__ == '__main__':
    main()
