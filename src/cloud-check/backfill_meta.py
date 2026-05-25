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
     ratio, dark_anomalous, dark_tiles, new_dark_tiles, photo_bucket,
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
    python backfill_meta.py [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
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
from db import BwFrame, Session

# Burst stages that suppress without running the background model.
_BURST_SUPPRESS_STAGES = frozenset({'DUPLICATE', 'BRIGHT_STABLE'})

_jpg_raw = os.getenv('JPG_FOLDER_PATH', '/tmp')
JPG_FOLDER = (_server_dir / _jpg_raw).resolve() if not Path(_jpg_raw).is_absolute() else Path(_jpg_raw)

# Fields that this script recomputes and overwrites every run.
_OVERWRITE_KEYS = (
    'tile_means', 'tile_means_u', 'tile_means_v',
    'model_tile_means', 'model_tile_means_u', 'model_tile_means_v',
    'result', 'stage', 'global_mean',
    'ratio', 'dark_anomalous', 'dark_tiles', 'new_dark_tiles', 'dark_blob_max',
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


def run_backfill(dry_run: bool = False) -> None:
    session = Session()

    frames = (session.query(BwFrame)
              .filter(BwFrame.filename.isnot(None))
              .order_by(BwFrame.captured_at.asc())
              .all())

    print(f"Found {len(frames)} frames with filenames", flush=True)
    print(f"JPG folder: {JPG_FOLDER}", flush=True)

    # Match firmware: 3 photo-buckets × 1 scene-bucket, warmup=4.
    # No centroid pre-seeding — the first 4 RTC frames per bucket warm the model.
    cc_cfg = Config(
        num_photo_buckets=3,
        num_scene_buckets=1,
        bright_photo_threshold=160,
        lowlight_photo_threshold=80,
        warmup_frames_per_bucket=4,
    )
    burst_cfg = BurstConfig()
    bg_model = BackgroundModel(cc_cfg)

    # Per-cell (photo_bucket × scene_bucket) previous tile means for temporal check
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

        # ── Feature extraction ────────────────────────────────────────────────
        # Prefer stored tile_means_y/u/v from DB (written by firmware or prev backfill).
        # Falls back to JPEG decode from local disk or HTTP.
        esp_tm_y = meta.get('tile_means')
        esp_tm_u = meta.get('tile_means_u')
        esp_tm_v = meta.get('tile_means_v')
        expected_tiles = cc_cfg.grid_h * cc_cfg.grid_w

        if esp_tm_y and isinstance(esp_tm_y, list) and len(esp_tm_y) == expected_tiles:
            tile_mean_y = np.array(esp_tm_y, dtype=np.float32).reshape(cc_cfg.grid_h, cc_cfg.grid_w)
            tile_mean_u = (
                np.array(esp_tm_u, dtype=np.float32).reshape(cc_cfg.grid_h, cc_cfg.grid_w)
                if esp_tm_u and len(esp_tm_u) == expected_tiles else None
            )
            tile_mean_v = (
                np.array(esp_tm_v, dtype=np.float32).reshape(cc_cfg.grid_h, cc_cfg.grid_w)
                if esp_tm_v and len(esp_tm_v) == expected_tiles else None
            )
            gm = int(float(tile_mean_y.mean()))
        else:
            jpg_path = JPG_FOLDER / frame.filename
            if jpg_path.exists():
                try:
                    y_arr, u_arr, v_arr = load_yuv_vga(jpg_path)
                    feats = extract_tile_features_yuv(y_arr, u_arr, v_arr)
                    tile_mean_y = feats['mean_y']
                    tile_mean_u = feats['mean_u']
                    tile_mean_v = feats['mean_v']
                    gm = feats['global_mean']
                except Exception as exc:
                    print(f"  ERR   {frame.filename}: {exc}", flush=True)
                    errors += 1
                    continue
            else:
                server_base = os.getenv('PHOTO_SERVER', 'http://192.168.1.110:8000').rstrip('/')
                url = f"{server_base}/static/{frame.filename}"
                try:
                    import io, requests
                    from PIL import Image as PILImage
                    resp = requests.get(url, timeout=15)
                    resp.raise_for_status()
                    ycbcr = PILImage.open(io.BytesIO(resp.content)).convert('YCbCr').resize((640, 480))
                    arr = np.asarray(ycbcr, dtype=np.uint8)
                    feats = extract_tile_features_yuv(arr[:, :, 0], arr[:, :, 1], arr[:, :, 2])
                    tile_mean_y = feats['mean_y']
                    tile_mean_u = feats['mean_u']
                    tile_mean_v = feats['mean_v']
                    gm = feats['global_mean']
                except Exception as exc:
                    print(f"  MISS  {frame.filename}: {exc}", flush=True)
                    errors += 1
                    continue

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
        bg_prev_y = prev_cell[0] if prev_cell is not None else None
        was_warmup = bg_model.warmup_remaining(pb, sb) > 0
        bg_model.observe(pb, sb)

        # Snapshot model means BEFORE any update — these are what z-scores were computed from
        model_means_y_flat: list[int] = bg_model.mean_y[pb, sb].flatten().round().astype(int).tolist()
        model_means_u_flat: list[int] = bg_model.mean_u[pb, sb].flatten().round().astype(int).tolist()
        model_means_v_flat: list[int] = bg_model.mean_v[pb, sb].flatten().round().astype(int).tolist()

        bg_pred = None
        burst_suppresses = (burst.label == 'suppress'
                            and burst.trigger in _BURST_SUPPRESS_STAGES)
        if burst_suppresses:
            result = 'clouds'
            stage = burst.trigger
        else:
            bg_pred = classify(
                tile_mean_y, bg_model, cc_cfg,
                prev_tile_mean=bg_prev_y,
                tile_mean_u=tile_mean_u,
                tile_mean_v=tile_mean_v,
            )
            result = bg_pred.label
            stage = bg_pred.trigger

            # Mirror firmware update policy: only RTC frames update the model.
            if meta.get('source') == 'rtc' and (
                was_warmup
                or bg_pred.label == 'clouds'
                or bg_pred.trigger in ('SCENE_DRIFT', 'NIGHT')
            ):
                bg_model.update(pb, sb, tile_mean_y, tile_mean_u, tile_mean_v)
            if bg_pred.trigger == 'SCENE_DRIFT' and meta.get('source') == 'rtc':
                bg_model.reset_warmup(pb, sb)

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
            new_fields['ratio']           = round(float(bg_pred.anomaly_ratio), 3)
            new_fields['new_dark_tiles']  = int(bg_pred.new_dark_tiles)
            new_fields['dark_anomalous']  = int(bg_pred.anomaly_mask.sum())
            new_fields['dark_tiles']      = int(bg_pred.dark_tiles)
            new_fields['dark_blob_max']   = int(bg_pred.dark_blob_max)
            new_fields['scene_bucket']    = int(bg_pred.scene_bucket)
            new_fields['n_chroma_changed'] = int(bg_pred.n_chroma_changed)
        else:
            new_fields['ratio']          = 0.0
            new_fields['new_dark_tiles'] = 0
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

    print(f'\nDone: {updated} updated, {errors} errors (missing JPG / decode fail)',
          flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description='Clean canonical recompute of pipeline meta from JPGs')
    ap.add_argument('--dry-run', action='store_true', help='Print changes without writing to DB')
    ap.add_argument('--force', action='store_true', help=argparse.SUPPRESS)
    args = ap.parse_args()
    run_backfill(dry_run=args.dry_run)


if __name__ == '__main__':
    main()
