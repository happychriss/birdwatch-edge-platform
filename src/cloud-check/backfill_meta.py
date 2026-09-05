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
"""backfill_meta.py — Clean canonical recompute of pipeline meta from JPGs.

For every bw_frames row that has a JPG file, this script:

  1. Loads the JPG and extracts tile_means_y/u/v + global_mean via BT.601 YCbCr.
  2. Replays the burst pre-filter + background-model pipeline against the
     JPG-derived tile_means, in strict chronological order, so the bg model
     evolves consistently across the dataset.
  3. Snapshots the model means BEFORE each frame's update and stores them
     as model_tile_means (drives the Δm tile overlay in the detail view).
  4. Overwrites: tile_means, tile_means_u, tile_means_v, model_tile_means,
     model_tile_means_u, model_tile_means_v, result, stage, global_mean,
     ratio, dark_anomalous, dark_tiles, dark_blob_max, tile_delta_luma,
     tile_delta_chroma, tile_color_mask, photo_bucket,
     burst_trigger, burst_label, burst_gm_diff, burst_n_changed, burst_n_dark,
     burst_n_chroma.
     Marks every reprocessed row with simulated=True.
  5. Preserves manual / external markers: label, downloaded_at, fresh_flash,
     fw_build (and any other meta keys the script does not touch).

Model update policy mirrors the firmware: only frames with source='rtc' update
the background model. PIR frames are evidence only (no model update).

Frames are processed unconditionally — the previous "already authoritative"
skip is gone.  When new firmware uploads land via /frame, the server keeps
the ESP-provided values; only this script overwrites them.

Usage:
    # Can be run from the dev container — shares the DB with production and
    # fetches JPGs from the photo server when not cached locally.
    # Frames that already have tile_means_u in meta (all current frames) need
    # no JPG fetch at all and run entirely from DB data.

    # Seed generation run (frames 604+, save converged model):
    python backfill_meta.py --from-frame 604 --save-seed model_seed.json

    # Production run (all frames, start from converged seed):
    python backfill_meta.py --load-seed model_seed.json

    # Explicit photo server (overrides PHOTO_SERVER env / default):
    python backfill_meta.py --photo-server http://192.168.1.110:8000 --load-seed model_seed.json

    # Dry-run preview:
    python backfill_meta.py --dry-run [--from-frame N] [--load-seed FILE]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# cloud_check lives in the same directory
_here = Path(__file__).parent
sys.path.insert(0, str(_here))

# db.py (SQLAlchemy models + connection) lives in ../python_bw_src/
_server_dir = _here.parent / 'python_bw_src'
sys.path.insert(0, str(_server_dir))

from dotenv import load_dotenv
load_dotenv(_server_dir / '.env')

import numpy as np
from sqlalchemy.orm.attributes import flag_modified

from cloud_check.burst_filter import BurstConfig, burst_classify
from cloud_check.background import BackgroundModel, photo_bucket_idx
from cloud_check.classifier import classify
from cloud_check.config import Config
from cloud_check.features import load_yuv_vga, extract_tile_features_yuv
from cloud_check.pipeline import BURST_SUPPRESS_STAGES
from db import BwFrame, Session

_jpg_raw = os.getenv('JPG_FOLDER_PATH', '/tmp')
JPG_FOLDER = (_server_dir / _jpg_raw).resolve() if not Path(_jpg_raw).is_absolute() else Path(_jpg_raw)

# Fields that this script recomputes and overwrites every run.
_OVERWRITE_KEYS = (
    'tile_means', 'tile_means_u', 'tile_means_v',
    'model_tile_means', 'model_tile_means_u', 'model_tile_means_v',
    'result', 'stage', 'global_mean',
    'ratio', 'dark_anomalous', 'dark_tiles', 'dark_blob_max',
    'tile_delta_luma', 'tile_delta_chroma', 'tile_color_mask',
    'photo_bucket', 'warmup', 'prev_valid', 'simulated',
    'burst_trigger', 'burst_label', 'burst_gm_diff',
    'burst_n_changed', 'burst_n_dark', 'burst_n_chroma',
)


def _photo_bucket(gm: int) -> str:
    """Map global Y mean to photo-bucket name (mirrors firmware thresholds)."""
    if gm >= 160:
        return 'BRIGHT'
    if gm < 80:
        return 'LOWLIGHT'
    return 'NORMAL'


def run_backfill(
    dry_run: bool = False,
    from_frame: int | None = None,
    save_seed: str | None = None,
    load_seed: str | None = None,
    photo_server: str | None = None,
    update_always: bool = False,
    ema_alpha: float | None = None,
) -> None:
    session = Session()

    all_frames = (session.query(BwFrame)
                  .filter(BwFrame.filename.isnot(None))
                  .order_by(BwFrame.captured_at.asc())
                  .all())

    print(f"Found {len(all_frames)} frames with filenames", flush=True)
    print(f"JPG folder: {JPG_FOLDER}", flush=True)

    # Single global model: affine normalization handles illumination continuously.
    cfg_kwargs: dict = dict(num_photo_buckets=1, num_scene_buckets=1, warmup_frames_per_bucket=4)
    if ema_alpha is not None:
        cfg_kwargs['ema_alpha'] = ema_alpha
    cc_cfg = Config(**cfg_kwargs)
    burst_cfg = BurstConfig()
    bg_model = BackgroundModel(cc_cfg)

    # ── Seed the background model ─────────────────────────────────────────────
    if load_seed:
        # Load a pre-converged model snapshot produced by a previous --save-seed run.
        # This gives a much better starting point than the corpus average, especially
        # for LOWLIGHT where very few RTC frames exist.
        seed_path = Path(load_seed)
        with seed_path.open() as f:
            seed_data = json.load(f)
        print(f"Loading seed from {seed_path} "
              f"(generated {seed_data.get('generated_at', '?')}, "
              f"source_frame_from={seed_data.get('source_frame_from', '?')})", flush=True)
        for pb_name, cell in seed_data['cells'].items():
            pb_idx = photo_bucket_idx(pb_name)
            if pb_idx >= cc_cfg.num_photo_buckets:
                continue  # seed has more buckets than model — skip
            mean_y = np.array(cell['mean_y'], dtype=np.float32).reshape(cc_cfg.grid_h, cc_cfg.grid_w)
            mean_u = np.array(cell.get('mean_u', [128] * cc_cfg.grid_h * cc_cfg.grid_w),
                              dtype=np.float32).reshape(cc_cfg.grid_h, cc_cfg.grid_w)
            mean_v = np.array(cell.get('mean_v', [128] * cc_cfg.grid_h * cc_cfg.grid_w),
                              dtype=np.float32).reshape(cc_cfg.grid_h, cc_cfg.grid_w)
            bg_model.seed_from_corpus(pb_idx, 0, mean_y, mean_u, mean_v)
            print(f"  Loaded {pb_name}: mean_y={mean_y.mean():.1f}", flush=True)
    else:
        # Corpus-average fallback: compute per-bucket per-tile mean from all DB
        # frames that have tile_means stored.  LOWLIGHT uses gm>=60 only to avoid
        # nighttime frames (gm<60) pulling the dusk model too dark.
        _LOWLIGHT_SEED_MIN_GM = 60
        _corpus_y: dict[str, list[np.ndarray]] = {}
        for f in all_frames:
            m = f.meta or {}
            gm_val = m.get('global_mean', 128)
            if gm_val < _LOWLIGHT_SEED_MIN_GM:
                continue  # exclude nighttime frames from seed regardless of bucket count
            pb = bg_model.photo_bucket_for(gm_val)
            tm = m.get('tile_means')
            if tm and len(tm) == cc_cfg.grid_h * cc_cfg.grid_w:
                _corpus_y.setdefault(pb, []).append(
                    np.array(tm, dtype=np.float32).reshape(cc_cfg.grid_h, cc_cfg.grid_w)
                )
        for pb_name, arrs in _corpus_y.items():
            pb_idx = photo_bucket_idx(pb_name)
            if pb_idx >= cc_cfg.num_photo_buckets:
                continue
            seed_y = np.stack(arrs).mean(axis=0)
            bg_model.seed_from_corpus(pb_idx, 0, seed_y)
            print(f"  Seeded {pb_name} from corpus ({len(arrs)} frames, tile mean={seed_y.mean():.1f})", flush=True)

    # ── Apply from-frame filter ───────────────────────────────────────────────
    if from_frame is not None:
        frames = [f for f in all_frames if f.id >= from_frame]
        print(f"Processing frames with id >= {from_frame}: {len(frames)} of {len(all_frames)}", flush=True)
    else:
        frames = all_frames

    # Per-cell (photo_bucket × scene_bucket) previous tile means — used to track
    # which cells received at least one real frame (for prev_valid flag).
    prev_tile_mean_by_cell: dict[tuple[int, int], tuple[np.ndarray, np.ndarray, np.ndarray]] = {}

    # Burst filter prev state (all frames, not just RTC)
    prev_burst_tile_mean_y: np.ndarray | None = None
    prev_burst_tile_mean_u: np.ndarray | None = None
    prev_burst_tile_mean_v: np.ndarray | None = None
    prev_burst_gm: float | None = None
    prev_burst_ts: datetime | None = None

    updated = errors = 0

    for frame in frames:
        meta = dict(frame.meta or {})

        # ── Feature extraction — always decode from JPG ───────────────────────
        # Canonical recompute: never trust stored tile_means from DB meta.
        # Priority: local file → HTTP fetch from photo server.
        jpg_path = JPG_FOLDER / frame.filename
        if jpg_path.exists():
            try:
                y_arr, u_arr, v_arr = load_yuv_vga(jpg_path)
                feats = extract_tile_features_yuv(y_arr, u_arr, v_arr)
            except Exception as exc:
                print(f"  ERR   {frame.filename}: {exc}", flush=True)
                errors += 1
                continue
        else:
            _server = (photo_server
                       or os.getenv('PHOTO_SERVER', 'http://192.168.1.110:8000')).rstrip('/')
            url = f"{_server}/static/{frame.filename}"
            try:
                import io
                import requests
                from PIL import Image as PILImage
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                ycbcr = PILImage.open(io.BytesIO(resp.content)).convert('YCbCr').resize((640, 480))
                arr = np.asarray(ycbcr, dtype=np.uint8)
                feats = extract_tile_features_yuv(arr[:, :, 0], arr[:, :, 1], arr[:, :, 2])
            except Exception as exc:
                print(f"  MISS  {frame.filename}: {exc}", flush=True)
                errors += 1
                continue

        tile_mean_y = feats['mean_y']
        tile_mean_u = feats['mean_u']
        tile_mean_v = feats['mean_v']
        tile_std_y  = feats['std_y']
        gm = feats['global_mean']

        # ── dt since previous frame ───────────────────────────────────────────
        if prev_burst_ts is not None and frame.captured_at is not None:
            dt = max(0.0, (frame.captured_at - prev_burst_ts).total_seconds())
        else:
            dt = float('inf')

        # ── Burst pre-filter ──────────────────────────────────────────────────
        burst = burst_classify(
            tile_mean_y, float(gm),
            prev_burst_tile_mean_y, prev_burst_gm,
            dt, burst_cfg,
            tile_mean_u=tile_mean_u,
            tile_mean_v=tile_mean_v,
            prev_tile_mean_u=prev_burst_tile_mean_u,
            prev_tile_mean_v=prev_burst_tile_mean_v,
        )

        # ── Background model ──────────────────────────────────────────────────
        pb_name = bg_model.photo_bucket_for(gm)
        pb = photo_bucket_idx(pb_name)
        sb = bg_model.scene_bucket_for(pb, tile_mean_y)
        prev_cell = prev_tile_mean_by_cell.get((pb, sb))
        was_warmup = bg_model.warmup_remaining(pb, sb) > 0
        bg_model.observe(pb, sb)

        # Snapshot model means BEFORE any update — these are what z-scores were computed from
        model_means_y_flat: list[int] = bg_model.mean_y[pb, sb].flatten().round().astype(int).tolist()
        model_means_u_flat: list[int] = bg_model.mean_u[pb, sb].flatten().round().astype(int).tolist()
        model_means_v_flat: list[int] = bg_model.mean_v[pb, sb].flatten().round().astype(int).tolist()

        bg_pred = None
        burst_suppresses = (burst.label == 'suppress'
                            and burst.trigger in BURST_SUPPRESS_STAGES)
        if burst_suppresses:
            result = 'clouds'
            stage = burst.trigger
        elif burst.skip_bg_model:
            # NIGHT: upload unconditionally, update model if RTC, skip classifier.
            result = 'process'
            stage = burst.trigger
            if meta.get('source') == 'rtc':
                bg_model.update(pb, sb, tile_mean_y, tile_mean_u, tile_mean_v)
        else:
            bg_pred = classify(
                tile_mean_y, bg_model, cc_cfg,
                tile_mean_u=tile_mean_u,
                tile_mean_v=tile_mean_v,
                tile_std_y=tile_std_y,
            )
            result = bg_pred.label
            stage = bg_pred.trigger

            # Mirror firmware update policy: only RTC frames update the model.
            _should_update = (was_warmup or bg_pred.label == 'clouds') if not update_always else True
            if meta.get('source') == 'rtc' and _should_update:
                bg_model.update(pb, sb, tile_mean_y, tile_mean_u, tile_mean_v)

            prev_tile_mean_by_cell[(pb, sb)] = (tile_mean_y, tile_mean_u, tile_mean_v)

        # Advance burst state for next iteration (all frames, not just RTC)
        prev_burst_tile_mean_y = tile_mean_y
        prev_burst_tile_mean_u = tile_mean_u
        prev_burst_tile_mean_v = tile_mean_v
        prev_burst_gm = float(gm)
        prev_burst_ts = frame.captured_at

        # ── Build canonical meta payload ──────────────────────────────────────
        tile_means_y_flat: list[int] = tile_mean_y.flatten().round().astype(int).tolist()
        tile_means_u_flat = (tile_mean_u.flatten().round().astype(int).tolist()
                             if tile_mean_u is not None else None)
        tile_means_v_flat = (tile_mean_v.flatten().round().astype(int).tolist()
                             if tile_mean_v is not None else None)

        new_fields: dict = {
            'tile_means':          tile_means_y_flat,
            'model_tile_means':    model_means_y_flat,
            'model_tile_means_u':  model_means_u_flat,
            'model_tile_means_v':  model_means_v_flat,
            'global_mean':         gm,
            'photo_bucket':        _photo_bucket(gm),
            'result':              result,
            'stage':               stage,
            'warmup':              bool(was_warmup),
            'prev_valid':          bool(prev_cell is not None),
            'burst_trigger':       burst.trigger,
            'burst_label':         burst.label,
            'burst_gm_diff':       round(burst.gm_diff, 1),
            'burst_n_changed':     int(burst.n_changed),
            'burst_n_dark':        int(burst.n_dark),
            'burst_n_chroma':      int(burst.n_chroma_changed),
            'simulated':           True,
        }
        if tile_means_u_flat is not None:
            new_fields['tile_means_u'] = tile_means_u_flat
        if tile_means_v_flat is not None:
            new_fields['tile_means_v'] = tile_means_v_flat

        if bg_pred is not None:
            new_fields['ratio']            = round(float(bg_pred.anomaly_ratio), 3)
            new_fields['dark_anomalous']   = int(bg_pred.anomaly_mask.sum())
            new_fields['dark_tiles']       = int(bg_pred.dark_tiles)
            new_fields['dark_blob_max']    = int(bg_pred.dark_blob_max)
            new_fields['texture_blob_max'] = int(bg_pred.texture_blob_max)
            new_fields['scene_bucket']     = int(bg_pred.scene_bucket)
            new_fields['n_chroma_changed'] = int(bg_pred.n_chroma_changed)
            # Tile overlay arrays: Δluma, Δchroma, and colour mask (0=none, 1=blue/dark_model, 2=red/dark_blob)
            new_fields['tile_delta_luma'] = bg_pred.tile_delta_luma.flatten().round().astype(int).tolist()
            if bg_pred.tile_delta_chroma is not None:
                new_fields['tile_delta_chroma'] = bg_pred.tile_delta_chroma.flatten().round(1).tolist()
            color = np.zeros(bg_pred.dark_tile_mask.shape, dtype=np.int8)
            color[bg_pred.dark_tile_mask] = 1
            color[bg_pred.dark_blob_mask] = 2
            new_fields['tile_color_mask'] = color.flatten().tolist()
        else:
            new_fields['ratio']          = 0.0
            new_fields['dark_anomalous'] = 0
            new_fields['dark_tiles']     = 0
            new_fields['dark_blob_max']  = 0

        # Merge: existing meta wins for manual/external keys (label, downloaded_at,
        # fresh_flash, fw_build, etc.); recomputed keys overwrite.
        patched = {**meta, **new_fields}

        ts_str = frame.captured_at.strftime('%Y-%m-%d %H:%M') if frame.captured_at else '?'
        src = meta.get('source', '?')
        print(f"  {'DRY' if dry_run else 'UPD'} {frame.filename} {ts_str} [{src}]"
              f"  burst={burst.trigger:<17} bg={stage:<12} result={result}", flush=True)

        if not dry_run:
            frame.meta = patched
            flag_modified(frame, 'meta')
            frame.result = result
            updated += 1

    if not dry_run and updated > 0:
        session.commit()
        print('\nCommitted.', flush=True)

    # ── Save model seed ───────────────────────────────────────────────────────
    if save_seed and not dry_run:
        seed_path = Path(save_seed)
        seed_out: dict = {
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'source_frame_from': from_frame,
            'grid_h': cc_cfg.grid_h,
            'grid_w': cc_cfg.grid_w,
            'cells': {},
        }
        from cloud_check.background import PHOTO_BUCKETS
        for pb_idx in range(cc_cfg.num_photo_buckets):
            pb_name = PHOTO_BUCKETS[pb_idx]
            seed_out['cells'][pb_name] = {
                'mean_y': bg_model.mean_y[pb_idx, 0].flatten().round(2).tolist(),
                'mean_u': bg_model.mean_u[pb_idx, 0].flatten().round(2).tolist(),
                'mean_v': bg_model.mean_v[pb_idx, 0].flatten().round(2).tolist(),
            }
            my = bg_model.mean_y[pb_idx, 0].mean()
            print(f"  Seed {pb_name}: mean_y={my:.1f}", flush=True)
        with seed_path.open('w') as f:
            json.dump(seed_out, f, indent=2)
        print(f"Seed saved → {seed_path}", flush=True)

    print(f'\nDone: {updated} updated, {errors} errors (missing JPG / decode fail)',
          flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description='Clean canonical recompute of pipeline meta from JPGs')
    ap.add_argument('--dry-run', action='store_true', help='Print changes without writing to DB')
    ap.add_argument('--from-frame', type=int, metavar='ID',
                    help='Process only frames with id >= ID (seed generation run)')
    ap.add_argument('--save-seed', metavar='FILE',
                    help='Write converged model state to FILE after processing (JSON)')
    ap.add_argument('--load-seed', metavar='FILE',
                    help='Load model seed from FILE instead of computing corpus averages')
    ap.add_argument('--photo-server', metavar='URL',
                    help='Override photo server URL for JPG fetch (default: PHOTO_SERVER env or http://192.168.1.110:8000)')
    ap.add_argument('--update-always', action='store_true',
                    help='Update model on every RTC frame regardless of result (default: only on warmup or quiet)')
    ap.add_argument('--ema-alpha', type=float, metavar='A', default=None,
                    help='Override EMA alpha for this run (e.g. 0.40 for fast burn-in seed generation)')
    ap.add_argument('--force', action='store_true', help=argparse.SUPPRESS)
    args = ap.parse_args()
    run_backfill(
        dry_run=args.dry_run,
        from_frame=args.from_frame,
        save_seed=args.save_seed,
        load_seed=args.load_seed,
        photo_server=args.photo_server,
        update_always=args.update_always,
        ema_alpha=args.ema_alpha,
    )


if __name__ == '__main__':
    main()
