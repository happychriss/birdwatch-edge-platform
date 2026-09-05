#!/usr/bin/env python3
"""Regime-keyed, colour-aware background-diff visualiser (experiment).

Successor to pixel_delta.py.  Two changes driven by the scene reality:

  1. REGIME MODEL BANK.  A single EMA background cannot hold both "hard-sunny"
     (sharp shadow edges, blown sky, high contrast) and "flat-overcast" (no
     shadows, diffuse) at once — averaging them matches neither, so every
     sunny<->cloudy switch throws a big spurious residual.  Affine / high-pass
     normalisation removes *smooth* light shifts but NOT the hard shadow edges
     that exist sunny and vanish overcast.  So we keep a small bank of per-pixel
     models, each keyed by a global lighting-regime descriptor, matched online,
     spawned on demand, capped (nearest-pair merge).  Time-of-day shadow drift
     is handled *within* a regime by the EMA tracking the 15-min RTC cadence —
     no explicit clock buckets (which would also fight seasonal sun-angle drift).

  2. COLOUR.  Each model holds per-pixel Y, U, V means (BT.601 via PIL YCbCr).
     A grey pigeon separates from green plants / grey sky on chroma; global
     brightness and white-balance drift are normalised out (affine on luma,
     median-offset on chroma) so only a *local* colour/luma anomaly survives.

Scoring is FRAME-vs-its-matched-MODEL (not neighbour-vs-neighbour):

  * an RTC frame scored against its regime model  == RTC-vs-RTC background check
    (target: minimal blobs — RTC frames are background, residual = model error).
  * a PIR frame scored against its regime model    == model-vs-PIR detection
    (a bird should leave 1-3 small blobs; a person a big one).

Only `source` (rtc/pir) and the human `bird` label are trusted.  The ESP
`result` (clouds/process) is an on-device estimate and is NOT used anywhere.

Optimisation metric = blob count / largest-blob area, reported split by source.
RTC blob count is the loss to drive down; bird frames must keep >=1 blob.

Usage:
    source .venv/bin/activate
    python regime_diff.py --from-frame 1877 --photo-server http://192.168.1.110:8000
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

_here = Path(__file__).parent
sys.path.insert(0, str(_here))
sys.path.insert(0, str(_here.parent / "python_bw_src"))
from dotenv import load_dotenv
load_dotenv(_here.parent / "python_bw_src" / ".env")
from db import BwFrame, Session  # noqa: E402

CACHE = _here / ".jpg_cache"
IGNORE_LABELS = {"ignore", "delete"}   # user flag: not a bird, disregard the frame entirely

# ── model / regime params (all overridable via CLI) ──────────────────────────
EMA_ALPHA       = 0.15   # per-pixel mean/var EMA rate (RTC frames only)
WARMUP          = 3      # RTC updates before a model is scored against
MAX_REGIMES     = 5      # cap on the bank; spawning past this merges nearest pair
SPAWN_DIST      = 0.18   # regime-descriptor distance beyond which a new model is spawned
CHROMA_WEIGHT   = 3.0    # weight on (dU^2+dV^2) relative to dY^2 in the combined residual
STD_SCALE       = 8.0    # plant-flutter mask: weight = exp(-(sqrt(var_y)/STD_SCALE)^2)
BLOB_THR        = 16.0   # combined-residual threshold (DN) for blob detection
BLOB_MIN_AREA   = 8      # px; ignore blobs smaller than this (noise speckle)


# ── descriptor: 3 global numbers, no hard-coded regions ──────────────────────
def regime_descriptor(y: np.ndarray) -> np.ndarray:
    """[ mean/255, std/64, bright-quartile/dark-quartile ratio /8 ].

    mean+std capture overall brightness & contrast; the quartile ratio captures
    dynamic range (sunny = bright sky over dark shadow -> high; overcast = flat).
    """
    flat = y.ravel()
    mean = float(flat.mean())
    std = float(flat.std())
    p25, p75 = np.percentile(flat, [25, 75])
    hi = float(flat[flat >= p75].mean())
    lo = float(flat[flat <= p25].mean())
    ratio = hi / max(lo, 1.0)
    return np.array([mean / 255.0, std / 64.0, ratio / 8.0], dtype=np.float32)


def fetch_jpg(filename: str, photo_server: str) -> bytes:
    CACHE.mkdir(exist_ok=True)
    local = CACHE / filename
    if local.exists():
        return local.read_bytes()
    url = f"{photo_server.rstrip('/')}/static/{filename}"
    data = urllib.request.urlopen(url, timeout=20).read()
    local.write_bytes(data)
    return data


def load_yuv(filename: str, photo_server: str, size):
    """Return (Y, U, V) float32 arrays, BT.601, U/V centred ~128."""
    im = Image.open(io.BytesIO(fetch_jpg(filename, photo_server))).convert("YCbCr")
    im = im.resize(size, Image.BILINEAR)
    arr = np.asarray(im, dtype=np.float32)
    return arr[..., 0], arr[..., 1], arr[..., 2]


def yuv_to_rgb(y, u, v):
    """BT.601 inverse, for displaying the (colour) frame / model mean."""
    yc, uc, vc = y, u - 128.0, v - 128.0
    r = yc + 1.402 * vc
    g = yc - 0.344136 * uc - 0.714136 * vc
    b = yc + 1.772 * uc
    return np.clip(np.stack([r, g, b], -1), 0, 255).astype(np.uint8)


def affine_fit(ref: np.ndarray, tgt: np.ndarray):
    """tgt ≈ a·ref + b global least squares; returns (a, b)."""
    r = ref.ravel()
    A = np.vstack([r, np.ones_like(r)]).T
    (a, b), *_ = np.linalg.lstsq(A, tgt.ravel(), rcond=None)
    return float(a), float(b)


def blobs(field: np.ndarray, thr: float, min_area: int):
    """Threshold |field|, 8-connect, drop tiny.  Returns (count, max_area, boxes, frac)."""
    mask = field > thr
    frac = float(mask.mean())
    lbl, n = ndimage.label(mask, structure=np.ones((3, 3), bool))
    if n == 0:
        return 0, 0, [], frac
    sizes = ndimage.sum(np.ones_like(lbl), lbl, index=np.arange(1, n + 1))
    boxes, kept = [], []
    for i, sz in enumerate(sizes, start=1):
        if sz < min_area:
            continue
        ys, xs = np.where(lbl == i)
        boxes.append((xs.min(), ys.min(), xs.max(), ys.max()))
        kept.append(sz)
    if not kept:
        return 0, 0, [], frac
    return len(kept), int(max(kept)), boxes, frac


# ── regime model bank ─────────────────────────────────────────────────────────
class Bank:
    def __init__(self, max_regimes, alpha, spawn_dist):
        self.models = []   # each: dict(cy,cu,cv,vy, desc, count)
        self.max = max_regimes
        self.alpha = alpha
        self.spawn_dist = spawn_dist

    def match(self, desc):
        """Return (index, distance) of nearest model, or (None, inf)."""
        if not self.models:
            return None, float("inf")
        d = [float(np.linalg.norm(desc - m["desc"])) for m in self.models]
        i = int(np.argmin(d))
        return i, d[i]

    def spawn(self, y, u, v, desc):
        self.models.append(dict(cy=y.copy(), cu=u.copy(), cv=v.copy(),
                                vy=np.zeros_like(y), desc=desc.copy(), count=1))
        if len(self.models) > self.max:
            self._merge_nearest()
        return len(self.models) - 1

    def update(self, i, y, u, v, desc):
        m = self.models[i]
        a = self.alpha
        dy = y - m["cy"]
        m["cy"] += a * dy
        m["cu"] += a * (u - m["cu"])
        m["cv"] += a * (v - m["cv"])
        m["vy"] = (1 - a) * (m["vy"] + a * dy * dy)
        m["desc"] += a * (desc - m["desc"])
        m["count"] += 1

    def _merge_nearest(self):
        best, bi, bj = float("inf"), 0, 1
        for i in range(len(self.models)):
            for j in range(i + 1, len(self.models)):
                d = float(np.linalg.norm(self.models[i]["desc"] - self.models[j]["desc"]))
                if d < best:
                    best, bi, bj = d, i, j
        a, b = self.models[bi], self.models[bj]
        na, nb = a["count"], b["count"]
        w = na + nb
        for k in ("cy", "cu", "cv", "vy"):
            a[k] = (a[k] * na + b[k] * nb) / w
        a["desc"] = (a["desc"] * na + b["desc"] * nb) / w
        a["count"] = w
        self.models.pop(bj)


def _heat(ax, data, vmax, title, boxes=None):
    im = ax.imshow(data, cmap="inferno", vmin=0, vmax=vmax)
    ax.set_title(title, fontsize=9)
    for box in (boxes or []):
        x0, y0, x1, y1 = box
        ax.add_patch(plt.Rectangle((x0, y0), x1 - x0, y1 - y0,
                                   edgecolor="lime", facecolor="none", lw=1.4))
    return im


def render(meta, y, u, v, model, ri, rdist, out_path, args):
    """model = matched model dict (pre-update) or None during warmup."""
    have = model is not None and model["count"] >= args.warmup
    if not have:
        if not args.no_render:
            fig, ax = plt.subplots(2, 3, figsize=(16, 9))
            for r in ax:
                for a_ in r:
                    a_.set_xticks([]); a_.set_yticks([])
            ax[0, 0].imshow(yuv_to_rgb(y, u, v))
            ax[0, 0].text(0.5, 0.5, "model warmup", transform=ax[0, 0].transAxes,
                          ha="center", va="center", color="gray")
            fig.suptitle(f"#{meta['id']}  regime warmup", fontsize=12)
            fig.tight_layout(); fig.savefig(out_path, dpi=80, bbox_inches="tight"); plt.close(fig)
        return dict(n_blob=0, max_blob=0, frac=0.0, regime=ri, rdist=round(rdist, 3))

    # luma: affine-normalise current to model (removes residual global gain/offset)
    a, b = affine_fit(model["cy"], y)
    dy = y - (a * model["cy"] + b)
    # chroma: remove a global cast diff (AWB re-balance), keep local anomalies
    du = (u - model["cu"]); du -= np.median(du)
    dv = (v - model["cv"]); dv -= np.median(dv)
    chroma = np.sqrt(du * du + dv * dv)
    combined = np.sqrt(dy * dy + args.chroma_weight * (du * du + dv * dv))
    if args.smooth > 1:   # average out 1px JPEG speckle; a real bird is many px
        combined = ndimage.uniform_filter(combined, args.smooth)
    std_y = np.sqrt(np.maximum(model["vy"], 0))
    mask_plant = np.exp(-(std_y / args.std_scale) ** 2)   # 1 on stable bg, ->0 on flutter
    # edge mask: suppress residual where the MODEL has a strong spatial gradient
    # (railing/frame lines). A bird appears over a region smooth in the model
    # (open floor/railing-top), so it survives; only static hard edges are killed.
    grad = np.hypot(*np.gradient(model["cy"]))
    grad = ndimage.maximum_filter(grad, 3)                # widen edges slightly
    mask_edge = np.exp(-(grad / args.edge_scale) ** 2)
    mask = mask_plant * mask_edge
    masked = combined * mask
    n_blob, max_blob, boxes, frac = blobs(masked, args.blob_thr, args.blob_min_area)
    if args.no_render:
        return dict(n_blob=n_blob, max_blob=max_blob, frac=round(frac, 4),
                    regime=ri, rdist=round(rdist, 3))

    badge = "  *** BIRD ***" if meta.get("label") == "bird" else (
        f"  [{meta['label']}]" if meta.get("label") else "")
    fig, ax = plt.subplots(2, 3, figsize=(16, 9))
    ax[0, 0].imshow(yuv_to_rgb(y, u, v))
    ax[0, 0].set_title(f"#{meta['id']} [{(meta['src'] or '?').upper()}] {meta['t']}{badge}", fontsize=9)
    ax[0, 1].imshow(yuv_to_rgb(model["cy"], model["cu"], model["cv"]))
    ax[0, 1].set_title(f"regime {ri} model mean (n={model['count']}, dist={rdist:.2f})", fontsize=9)
    _heat(ax[0, 2], np.abs(dy), args.vmax, f"|luma residual|  a={a:.2f} b={b:+.0f}")
    _heat(ax[1, 0], chroma, args.vmax, "|chroma residual| (AWB-offset removed)")
    ax[1, 1].imshow(mask, cmap="gray", vmin=0, vmax=1)
    ax[1, 1].set_title("suppression mask (plant x edge)\nblack = suppressed", fontsize=9)
    im = _heat(ax[1, 2], masked, args.vmax,
               f"masked combined residual\nblobs={n_blob} max={max_blob}px frac={frac*100:.2f}%",
               boxes=boxes)
    fig.colorbar(im, ax=ax[1, 2], fraction=0.046, pad=0.04)

    for r in ax:
        for a_ in r:
            a_.set_xticks([]); a_.set_yticks([])
    fig.suptitle(f"#{meta['id']} [{(meta['src'] or '?').upper()}]  regime {ri}  "
                 f"blobs={n_blob}{badge}", fontsize=12)
    fig.tight_layout(); fig.savefig(out_path, dpi=80, bbox_inches="tight"); plt.close(fig)
    return dict(n_blob=n_blob, max_blob=max_blob, frac=round(frac, 4),
                regime=ri, rdist=round(rdist, 3))


def write_gallery(out: Path, rows):
    items = []
    for r in rows:
        bird = str(r.get("is_bird", "0")) == "1"
        lbl = ""
        if r.get("label") == "bird":
            lbl = " &nbsp; <b style='color:#4f4'>BIRD</b>"
        elif r.get("label"):
            lbl = f" &nbsp; <span style='color:#888'>[{html.escape(str(r['label']))}]</span>"
        cap = (f"#{r['id']} &nbsp; <b>{(r['src'] or '?').upper()}</b> &nbsp; {r['t']}{lbl}"
               f" &nbsp; | &nbsp; regime {r['regime']} (dist={r['rdist']}) &nbsp; "
               f"<b>blobs={r['n_blob']}</b> max={r['max_blob']}px frac={float(r['frac'])*100:.2f}%")
        items.append(dict(file=r["file"], cap=cap, src=(r["src"] or "?"),
                          bird="1" if bird else "0"))
    data_json = json.dumps(items)
    doc = """<!doctype html><meta charset=utf-8><title>regime-diff viewer</title>
<style>
 html,body{background:#111;color:#ddd;font:13px system-ui,sans-serif;margin:0;height:100%}
 .bar{padding:8px 12px;display:flex;gap:6px;align-items:center;flex-wrap:wrap;border-bottom:1px solid #333}
 button{background:#222;color:#ddd;border:1px solid #444;padding:5px 10px;border-radius:5px;cursor:pointer}
 button.on{background:#2a6;color:#000;border-color:#2a6}
 #pos{margin-left:auto;font-family:ui-monospace,monospace;color:#9c9}
 #stage{padding:10px 12px}
 #img{width:100%;max-width:1500px;display:block;cursor:pointer;user-select:none}
 #stage.bird-active #img{border:4px solid #e44;box-shadow:0 0 24px #e44}
 #cap{margin-top:6px;color:#bbb;font-family:ui-monospace,monospace}
 .hint{color:#777;margin-left:6px}
</style>
<div class=bar>
 <button id=prev>&#9664; Prev</button>
 <button id=next>Next &#9654;</button>
 <span class=hint>(click image or &larr; &rarr; keys)</span>
 &nbsp;|&nbsp; filter:
 <button data-f="all" class=on>all</button>
 <button data-f="src:rtc">RTC (bg check)</button>
 <button data-f="src:pir">PIR (detection)</button>
 <button data-f="bird:1" style="background:#193;color:#cfc;border-color:#4a4">&#128038; bird frames</button>
 <span id=pos></span>
</div>
<div id=stage><img id=img><div id=cap></div></div>
<script>
 const ALL = __DATA__;
 let view = ALL.slice(), i = 0;
 const img=document.getElementById('img'), cap=document.getElementById('cap'), pos=document.getElementById('pos');
 function show(){
   if(!view.length){img.removeAttribute('src');cap.innerHTML='<em>(none)</em>';pos.textContent='';return;}
   i=(i%view.length+view.length)%view.length;
   const it=view[i]; img.src=it.file; cap.innerHTML=it.cap;
   document.getElementById('stage').classList.toggle('bird-active', it.bird==='1');
   pos.textContent=`${i+1} / ${view.length}`;
 }
 const step=d=>{i+=d;show();};
 document.getElementById('next').onclick=()=>step(1);
 document.getElementById('prev').onclick=()=>step(-1);
 img.onclick=e=>step(e.offsetX < img.clientWidth/2 ? -1 : 1);
 document.addEventListener('keydown',e=>{
   if(e.key==='ArrowRight'||e.key===' ')step(1);
   else if(e.key==='ArrowLeft')step(-1);
 });
 document.querySelectorAll('button[data-f]').forEach(b=>b.onclick=()=>{
   document.querySelectorAll('button[data-f]').forEach(x=>x.classList.remove('on')); b.classList.add('on');
   const f=b.dataset.f;
   if(f==='all') view=ALL.slice();
   else { const [k,v]=f.split(':');
     if(k==='src') view=ALL.filter(it=>it.src===v);
     else if(k==='bird') view=ALL.filter(it=>it.bird===v); }
   i=0; show();
 });
 show();
</script>"""
    (out / "gallery.html").write_text(doc.replace("__DATA__", data_json))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-frame", type=int, default=1877)
    ap.add_argument("--to-frame", type=int, default=None)
    ap.add_argument("--photo-server", default=os.getenv("PHOTO_SERVER", "http://192.168.1.110:8000"))
    ap.add_argument("--out", default=str(_here / "regime_diff_out"))
    ap.add_argument("--width", type=int, default=800)
    ap.add_argument("--vmax", type=float, default=48.0)
    ap.add_argument("--alpha", type=float, default=EMA_ALPHA)
    ap.add_argument("--warmup", type=int, default=WARMUP)
    ap.add_argument("--max-regimes", type=int, default=MAX_REGIMES)
    ap.add_argument("--spawn-dist", type=float, default=SPAWN_DIST)
    ap.add_argument("--chroma-weight", type=float, default=CHROMA_WEIGHT)
    ap.add_argument("--std-scale", type=float, default=STD_SCALE)
    ap.add_argument("--blob-thr", type=float, default=BLOB_THR)
    ap.add_argument("--blob-min-area", type=int, default=BLOB_MIN_AREA)
    ap.add_argument("--smooth", type=int, default=3, help="box-filter (px) on residual before blobs")
    ap.add_argument("--edge-scale", type=float, default=12.0, help="edge mask: exp(-(model_grad/scale)^2)")
    ap.add_argument("--no-render", action="store_true", help="compute blobs+stats+csv only, skip PNGs (fast sweep)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--gallery-only", action="store_true")
    args = ap.parse_args()

    size = (args.width, int(args.width * 3 / 4))
    out = Path(args.out); out.mkdir(exist_ok=True)

    if args.gallery_only:
        rows = list(csv.DictReader(open(out / "index.csv")))
        write_gallery(out, rows)
        print(f"Rebuilt gallery.html from {len(rows)} rows")
        return

    s = Session()
    q = s.query(BwFrame).filter(BwFrame.id >= args.from_frame).filter(BwFrame.filename.isnot(None))
    if args.to_frame:
        q = q.filter(BwFrame.id <= args.to_frame)
    frames = q.order_by(BwFrame.captured_at.asc()).all()
    print(f"{len(frames)} frames; size {size}; alpha={args.alpha} warmup={args.warmup} "
          f"K<={args.max_regimes} spawn={args.spawn_dist} chroma_w={args.chroma_weight} "
          f"std_scale={args.std_scale} blob_thr={args.blob_thr}")

    bank = Bank(args.max_regimes, args.alpha, args.spawn_dist)
    rows = []
    n = 0
    for f in frames:
        src = (f.meta or {}).get("source")
        label = (f.meta or {}).get("label") or ""
        is_ignore = label in IGNORE_LABELS
        meta = dict(id=f.id, src=src, label=label, t=f.captured_at.strftime("%m-%d %H:%M:%S"))
        try:
            y, u, v = load_yuv(f.filename, args.photo_server, size)
        except Exception as exc:
            print(f"  MISS #{f.id} {f.filename}: {exc}")
            continue
        desc = regime_descriptor(y)
        ri, rdist = bank.match(desc)
        # match for scoring: if nothing close enough yet, score against nearest anyway
        # (flagged by rdist); model only created/updated on RTC frames.
        score_model = bank.models[ri] if ri is not None else None
        # snapshot the model BEFORE updating so an RTC frame is scored against its
        # prior state (true background-prediction error, not a self-fit).
        snap = None
        if score_model is not None:
            snap = dict(cy=score_model["cy"].copy(), cu=score_model["cu"].copy(),
                        cv=score_model["cv"].copy(), vy=score_model["vy"].copy(),
                        count=score_model["count"])

        name = f"{n:04d}_{(src or 'x').upper()}_{f.id}{'_BIRD' if label == 'bird' else ''}.png"
        m = render(meta, y, u, v, snap, ri if ri is not None else -1, rdist, out / name, args)
        m.update(file=name, id=f.id, src=src, label=label, t=meta["t"],
                 is_bird=1 if label == "bird" else 0,
                 is_ignore=1 if is_ignore else 0)
        rows.append(m)

        # RTC frames build the bank; PIR never mutates a model.
        # ignore/delete frames never touch the model (may contain a person/junk).
        if src == "rtc" and not is_ignore:
            if ri is None or rdist > args.spawn_dist:
                ri = bank.spawn(y, u, v, desc)
            else:
                bank.update(ri, y, u, v, desc)
        n += 1
        if n % 50 == 0:
            print(f"  {n}/{len(frames)}  #{f.id} regime={ri} blobs={m['n_blob']} (K={len(bank.models)})")
        if args.limit and n >= args.limit:
            break

    # summary split by source (RTC blob count = optimisation target)
    def stat(sel):
        bl = [r["n_blob"] for r in rows
              if sel(r) and r["regime"] >= 0 and not r.get("is_ignore")]
        return (len(bl), np.mean(bl) if bl else 0, sum(1 for x in bl if x == 0))
    rtc_n, rtc_mean, rtc_zero = stat(lambda r: r["src"] == "rtc")
    pir_n, pir_mean, _ = stat(lambda r: r["src"] == "pir")
    bird_rows = [r for r in rows if r["is_bird"] and r["regime"] >= 0]
    bird_hit = sum(1 for r in bird_rows if r["n_blob"] >= 1)

    cols = ["file", "id", "src", "t", "label", "is_bird", "is_ignore", "regime", "rdist",
            "n_blob", "max_blob", "frac"]
    with open(out / "index.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols); w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in cols})
    write_gallery(out, rows)

    print(f"\nWrote {len(rows)} panels + index.csv + gallery.html to {out}")
    print(f"final regimes: {len(bank.models)}  "
          f"centroids={[ [round(float(x),2) for x in m['desc']] for m in bank.models ]}")
    print(f"RTC (bg check, scored): n={rtc_n}  mean blobs={rtc_mean:.2f}  "
          f"clean(0 blobs)={rtc_zero}/{rtc_n}  ({100*rtc_zero/max(rtc_n,1):.0f}%)   <- minimise")
    print(f"PIR (detection):        n={pir_n}  mean blobs={pir_mean:.2f}")
    print(f"BIRD frames scored:     {len(bird_rows)}  with >=1 blob: {bird_hit}/{len(bird_rows)}")


if __name__ == "__main__":
    main()
