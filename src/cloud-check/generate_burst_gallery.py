"""generate_burst_gallery.py — HTML gallery of all training frames in chronological order.

Combines all folders (sun-shining + process-*), sorts by filename timestamp,
runs burst_classify on each, and produces a self-contained HTML gallery showing
thumbnails with color-coded classification decisions.

    python generate_burst_gallery.py [output.html]
"""

from __future__ import annotations

import base64
import io
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from cloud_check.burst_filter import BurstConfig, BurstResult, burst_classify
from cloud_check.features import FRAME_W, FRAME_H, GRID_W, GRID_H

_candidates = [Path('/workspace/training-data'), Path(__file__).parents[2] / 'training-data']
TRAINING_DATA = next((p for p in _candidates if p.exists()), _candidates[0])

ALL_FOLDERS = {
    'ignore-sun_shining': 'sun',
    'process-birds-pillow': 'process',
    'process-people': 'process',
    'process-dark': 'process',
    'process-real-birds': 'process',
}

THUMB_W, THUMB_H = 192, 144


def load_tile_means(path: Path) -> tuple[np.ndarray, float]:
    with Image.open(path) as im:
        gray = im.convert('L').resize((FRAME_W, FRAME_H), Image.Resampling.BILINEAR)
        arr = np.asarray(gray, dtype=np.float32)
    tile_h = FRAME_H // GRID_H
    tile_w = FRAME_W // GRID_W
    tiles = arr[:GRID_H * tile_h, :GRID_W * tile_w]
    tiles = tiles.reshape(GRID_H, tile_h, GRID_W, tile_w).transpose(0, 2, 1, 3)
    means = tiles.reshape(GRID_H, GRID_W, -1).mean(axis=2)
    return means, float(arr.mean())


def make_thumb_b64(path: Path) -> str:
    with Image.open(path) as im:
        im.thumbnail((THUMB_W, THUMB_H), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format='JPEG', quality=72)
    return base64.b64encode(buf.getvalue()).decode()


def ts_from_name(name: str) -> datetime:
    try:
        return datetime.strptime(name[:15], '%Y%m%d_%H%M%S')
    except ValueError:
        return datetime.min


@dataclass
class FrameRecord:
    path: Path
    folder: str
    true_label: str
    ts: datetime
    tile_mean: np.ndarray
    gm: float
    dt: float
    result: BurstResult
    gm_diff: float
    thumb_b64: str


def gather_all_frames() -> list[FrameRecord]:
    entries: list[tuple[datetime, Path, str, str]] = []
    for folder_name, label in ALL_FOLDERS.items():
        folder = TRAINING_DATA / folder_name
        if not folder.exists():
            continue
        for p in folder.iterdir():
            if p.suffix.lower() == '.jpg':
                ts = ts_from_name(p.name)
                entries.append((ts, p, folder_name, label))

    entries.sort(key=lambda x: x[0])

    cfg = BurstConfig()
    records: list[FrameRecord] = []
    prev_tile_mean = None
    prev_gm = None
    prev_ts = None

    for ts, path, folder_name, true_label in entries:
        tile_mean, gm = load_tile_means(path)

        if prev_ts is None or prev_tile_mean is None:
            dt = float('inf')
        else:
            raw_dt = (ts - prev_ts).total_seconds()
            dt = float('inf') if raw_dt < 0 else raw_dt

        result = burst_classify(tile_mean, gm, prev_tile_mean, prev_gm, dt, cfg)
        gm_diff = abs(gm - prev_gm) if prev_gm is not None else 0.0

        print(f"  [{ts.strftime('%m-%d %H:%M:%S')}] {folder_name}/{path.name}  → {result.label}", flush=True)
        thumb = make_thumb_b64(path)

        records.append(FrameRecord(
            path=path, folder=folder_name, true_label=true_label,
            ts=ts, tile_mean=tile_mean, gm=gm, dt=dt,
            result=result, gm_diff=gm_diff, thumb_b64=thumb,
        ))

        prev_tile_mean = tile_mean
        prev_gm = gm
        prev_ts = ts

    return records


def card_color(r: FrameRecord) -> str:
    label = r.result.label
    expected = 'suppress' if r.true_label == 'sun' else 'process'
    correct = label == expected

    if r.true_label == 'sun':
        return '#2d6a2d' if correct else '#8b6914'  # green=suppressed, amber=missed sun
    else:
        if correct:
            return '#1a3a5c'  # blue=correctly processed
        else:
            # error: was suppressed but shouldn't be
            if 'bird' in r.folder:
                return '#8b1a1a'  # red = bird/pillow error (critical)
            return '#6b2d6b'  # purple = people error (acceptable)


def label_badge(r: FrameRecord) -> str:
    label = r.result.label
    expected = 'suppress' if r.true_label == 'sun' else 'process'
    correct = label == expected
    bg = '#4caf50' if correct else '#f44336'
    return f'<span style="background:{bg};color:#fff;padding:2px 6px;border-radius:3px;font-size:11px;">{label.upper()}</span>'


def render_html(records: list[FrameRecord]) -> str:
    cards = []
    for r in records:
        bg = card_color(r)
        badge = label_badge(r)
        dt_str = '∞' if r.dt == float('inf') else f'{r.dt:.0f}s'
        ts_str = r.ts.strftime('%Y-%m-%d %H:%M:%S')

        cards.append(f'''
<div class="card" style="background:{bg};">
  <img src="data:image/jpeg;base64,{r.thumb_b64}" width="{THUMB_W}" height="{THUMB_H}" loading="lazy">
  <div class="info">
    <div class="ts">{ts_str}</div>
    <div class="folder">{r.folder}</div>
    <div class="metrics">dt={dt_str}  gm={r.gm:.0f}  Δgm={r.gm_diff:+.1f}</div>
    <div class="metrics">n={r.result.n_changed}  nd={r.result.n_dark}  blob={r.result.blob_max}</div>
    <div class="trigger">{r.result.trigger}</div>
    {badge}
  </div>
</div>''')

    cfg = BurstConfig()
    n_total = len(records)
    n_suppressed = sum(1 for r in records if r.result.label == 'suppress')
    n_correct = sum(1 for r in records
                    if r.result.label == ('suppress' if r.true_label == 'sun' else 'process'))
    n_bird_errors = sum(1 for r in records
                        if r.true_label == 'process' and r.result.label == 'suppress'
                        and 'bird' in r.folder)
    n_people_errors = sum(1 for r in records
                          if r.true_label == 'process' and r.result.label == 'suppress'
                          and 'people' in r.folder)

    cards_html = '\n'.join(cards)
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Burst Filter Gallery</title>
<style>
  body {{ background:#111; color:#ddd; font-family:monospace; margin:0; padding:12px; }}
  h1 {{ color:#eee; margin-bottom:4px; }}
  .summary {{ background:#222; padding:10px 16px; border-radius:6px; margin-bottom:16px;
              display:flex; gap:24px; flex-wrap:wrap; }}
  .stat {{ display:flex; flex-direction:column; }}
  .stat .val {{ font-size:20px; font-weight:bold; color:#fff; }}
  .stat .lbl {{ font-size:11px; color:#888; }}
  .cfg {{ background:#1a1a2e; padding:8px 14px; border-radius:4px; font-size:11px;
          margin-bottom:16px; color:#9ab; }}
  .legend {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:16px; font-size:11px; }}
  .legend span {{ padding:3px 10px; border-radius:3px; }}
  .gallery {{ display:flex; flex-wrap:wrap; gap:8px; }}
  .card {{ border-radius:6px; overflow:hidden; width:{THUMB_W}px; flex-shrink:0; }}
  .card img {{ display:block; }}
  .info {{ padding:4px 6px 6px; }}
  .ts {{ font-size:10px; color:#ccc; }}
  .folder {{ font-size:10px; color:#aaa; margin-bottom:2px; white-space:nowrap;
             overflow:hidden; text-overflow:ellipsis; }}
  .metrics {{ font-size:10px; color:#bbb; }}
  .trigger {{ font-size:10px; color:#e8c96a; margin:2px 0; }}
</style>
</head>
<body>
<h1>Burst Filter — Chronological Gallery</h1>
<div class="cfg">
  BurstConfig: burst_window={cfg.burst_window_seconds:.0f}s &nbsp;|&nbsp;
  brightness_sim={cfg.brightness_sim_threshold:.0f} DN &nbsp;|&nbsp;
  tile_diff={cfg.tile_diff_threshold:.0f} DN &nbsp;|&nbsp;
  dark_diff={cfg.dark_diff_threshold:.0f} DN &nbsp;|&nbsp;
  diffuse_min_dark={cfg.diffuse_min_dark_tiles} tiles
</div>
<div class="summary">
  <div class="stat"><span class="val">{n_total}</span><span class="lbl">total frames</span></div>
  <div class="stat"><span class="val">{n_suppressed}</span><span class="lbl">suppressed</span></div>
  <div class="stat"><span class="val">{n_correct}</span><span class="lbl">correct</span></div>
  <div class="stat"><span class="val" style="color:#f44">{n_bird_errors}</span><span class="lbl">bird errors (critical)</span></div>
  <div class="stat"><span class="val" style="color:#c8f">{n_people_errors}</span><span class="lbl">people suppressed (acceptable)</span></div>
</div>
<div class="legend">
  <span style="background:#2d6a2d;">■ sun suppressed ✓</span>
  <span style="background:#8b6914;">■ sun missed (processed)</span>
  <span style="background:#1a3a5c;">■ process ✓</span>
  <span style="background:#8b1a1a;">■ BIRD ERROR (suppressed)</span>
  <span style="background:#6b2d6b;">■ people suppressed (ok)</span>
</div>
<div class="gallery">
{cards_html}
</div>
</body>
</html>'''


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else _here / 'reports' / 'burst_gallery.html'
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"Processing frames from {TRAINING_DATA} ...")
    records = gather_all_frames()
    print(f"\nRendering {len(records)} cards → {out}")
    html = render_html(records)
    out.write_text(html)
    print(f"Done. Open: {out}")


if __name__ == '__main__':
    main()
