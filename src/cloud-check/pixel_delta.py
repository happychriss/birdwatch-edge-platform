#!/usr/bin/env python3
"""Pixel-level frame-to-frame delta visualiser (experimental).

For a range of bw_frames (default id>=1878, the new-camera-settings era), fetch
the full-res JPEGs from the photo server and render, for every *consecutive*
chronological pair, an 8-panel comparison:

  row 1:  [ prev ] [ curr ] [ |raw Δ| ]          [ |affine residual| ]
  row 2:  [ |HP delta| ] [ RTC model mean ] [ per-pixel std (plant map) ] [ masked residual ]

Layers, weakest-to-strongest illumination handling:

  * raw Δ              curr-prev. Everything, including global cloud/exposure shift.
  * affine residual    fit one global line a·prev+b≈curr, subtract. Removes a *uniform*
                       light shift; fails when sky and foreground dim at different rates.
  * HP delta           high-pass each frame (x - boxblur(x)), then diff. Removes any
                       *smooth* gradient locally, so a two-slope light swing also cancels;
                       only hard local structure survives.
  * model residual     per-pixel running background (EMA over RTC frames only), then
                       affine-normalise curr to the model and subtract. The background is
                       learned, not a single neighbour frame.
  * masked residual    the model residual with high-temporal-variance pixels (plants that
                       flutter every frame) suppressed via the per-pixel std map. A coherent
                       object on otherwise-stable background is what is left.

Each pair is labelled with the transition (RTC→RTC, RTC→PIR, …), the time gap and the
stored result. Writes one PNG per pair, an index.csv of scalar metrics, and gallery.html.

Usage:
    source ../python_bw_src/.venv/bin/activate   # or this dir's .venv
    python pixel_delta.py --from-frame 1878 --photo-server http://192.168.1.110:8000
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
BT601 = np.array([0.299, 0.587, 0.114], dtype=np.float32)

# ── EMA background-model params (RTC frames only) ─────────────────────────────
EMA_ALPHA = 0.15        # per-pixel mean/variance EMA rate (steady state)
FAST_ALPHA = 0.50       # alpha used after a scene break (camera reposition); converges in ~5 frames
N_FAST_FRAMES = 7       # RTC frames to use FAST_ALPHA after a scene break
SCENE_BREAK_THR = 30.0  # DN; mean |HP(curr) - hp_model| above this → camera repositioned → reset model
MODEL_WARMUP = 3        # RTC frames before model panels are shown
STD_MASK_SCALE = 4.0    # DN; plant suppression weight = exp(-(std/scale)^2)
HP_KERNEL = 25          # box-blur window (px) for the high-pass


def fetch_jpg(filename: str, photo_server: str) -> bytes:
    CACHE.mkdir(exist_ok=True)
    local = CACHE / filename
    if local.exists():
        return local.read_bytes()
    url = f"{photo_server.rstrip('/')}/static/{filename}"
    data = urllib.request.urlopen(url, timeout=20).read()
    local.write_bytes(data)
    return data


def load_luma(filename: str, photo_server: str, size: tuple[int, int]) -> np.ndarray:
    im = Image.open(io.BytesIO(fetch_jpg(filename, photo_server))).convert("RGB")
    im = im.resize(size, Image.BILINEAR)
    return np.asarray(im, dtype=np.float32) @ BT601


def affine_fit(ref: np.ndarray, tgt: np.ndarray) -> tuple[np.ndarray, float, float]:
    """Fit tgt ≈ a·ref + b (global least squares); return residual, a, b."""
    r = ref.ravel()
    A = np.vstack([r, np.ones_like(r)]).T
    (a, b), *_ = np.linalg.lstsq(A, tgt.ravel(), rcond=None)
    return tgt - (a * ref + b), float(a), float(b)


def high_pass(img: np.ndarray, k: int) -> np.ndarray:
    return img - ndimage.uniform_filter(img, size=k, mode="reflect")


def largest_blob(field: np.ndarray, thr: float):
    """Threshold |field|, 8-connect; return (area, bbox, n_blobs, frac)."""
    mask = np.abs(field) > thr
    frac = float(mask.mean())
    lbl, n = ndimage.label(mask, structure=np.ones((3, 3), bool))
    if n == 0:
        return 0, None, 0, frac
    sizes = ndimage.sum(np.ones_like(lbl), lbl, index=np.arange(1, n + 1))
    big = int(np.argmax(sizes)) + 1
    ys, xs = np.where(lbl == big)
    return int(sizes.max()), (xs.min(), ys.min(), xs.max(), ys.max()), n, frac


def transition(a: str, b: str) -> str:
    return f"{(a or '??').upper()}→{(b or '??').upper()}"


def _heat(ax, data, vmax, title, box=None):
    im = ax.imshow(data, cmap="inferno", vmin=0, vmax=vmax)
    ax.set_title(title, fontsize=9)
    if box is not None:
        x0, y0, x1, y1 = box
        ax.add_patch(plt.Rectangle((x0, y0), x1 - x0, y1 - y0,
                                    edgecolor="lime", facecolor="none", lw=1.6))
    return im


def render_pair(prev, curr, mp, mc, out_path, vmax, thr, model):
    """model = dict(mean, std, count) or None during warmup."""
    raw = curr - prev
    aff, a, b = affine_fit(prev, curr)
    hp = high_pass(curr, HP_KERNEL) - high_pass(prev, HP_KERNEL)

    raw_mean = float(np.abs(raw).mean())
    aff_mean = float(np.abs(aff).mean())
    hp_mean = float(np.abs(hp).mean())
    hp_area, hp_box, _, _ = largest_blob(hp, thr)

    have_model = model is not None and model["count"] >= MODEL_WARMUP
    if have_model:
        # residual in HP space: HP removes global illumination gradients, no affine fit needed.
        # Not masked for now — HP-std dynamic range (2-8 DN) is too narrow with ~50 RTC frames
        # to discriminate plant flutter from stable background. Show unmasked so signal is visible.
        hp_curr = high_pass(curr, HP_KERNEL)
        mres = hp_curr - model["hp_mean"]
        masked = mres   # label says "masked" in CSV but it's unmasked until more data available
        masked_mean = float(np.abs(masked).mean())
        mk_area, mk_box, mk_n, mk_frac = largest_blob(masked, thr)
        std_mean = float(model["std"].mean())
    else:
        masked_mean = mk_area = mk_n = std_mean = 0
        mk_frac = 0.0
        mk_box = None

    fig, ax = plt.subplots(2, 4, figsize=(20, 9.2))
    ax[0, 0].imshow(prev, cmap="gray", vmin=0, vmax=255)
    ax[0, 0].set_title(f"prev #{mp['id']} [{(mp['src'] or '?').upper()}]  {mp['t']}\n"
                       f"result={mp['result']}", fontsize=9)
    ax[0, 1].imshow(curr, cmap="gray", vmin=0, vmax=255)
    label_badge = f"  *** BIRD ***" if mc.get("label") == "bird" else (f"  [{mc.get('label')}]" if mc.get("label") else "")
    ax[0, 1].set_title(f"curr #{mc['id']} [{(mc['src'] or '?').upper()}]  {mc['t']}\n"
                       f"result={mc['result']}{label_badge}", fontsize=9)
    _heat(ax[0, 2], np.abs(raw), vmax, f"|raw Δ|  mean={raw_mean:.1f}\n(global shift included)")
    _heat(ax[0, 3], np.abs(aff), vmax, f"|affine residual|  mean={aff_mean:.1f}\n"
          f"a={a:.2f} b={b:+.0f}  (one global line)")

    _heat(ax[1, 0], np.abs(hp), vmax, f"|HP delta|  mean={hp_mean:.1f}\n"
          f"local-contrast (k={HP_KERNEL})", box=hp_box if hp_area >= 20 else None)
    if have_model:
        ax[1, 1].imshow(model["mean"], cmap="gray", vmin=0, vmax=255)
        fast = model.get("fast_left", 0)
        alpha_tag = f"  α={FAST_ALPHA} fast({fast} left)" if fast > 0 else f"  α={EMA_ALPHA}"
        ax[1, 1].set_title(f"RTC model mean (n={model['count']}{alpha_tag})", fontsize=9)
        _heat(ax[1, 2], model["std"], 30.0, f"per-pixel HP-std  mean={std_mean:.1f}\n"
              "(std of HP frames: bright = local flutter only, not global illumination)")
        im = _heat(ax[1, 3], np.abs(masked), vmax, f"|HP(curr) − HP-model|  mean={masked_mean:.1f}\n"
                   f"frac={mk_frac*100:.1f}% blob={mk_area}px  (std map = reference, not applied yet)",
                   box=mk_box if mk_area >= 20 else None)
        fig.colorbar(im, ax=ax[1, 3], fraction=0.046, pad=0.04)
    else:
        for j, msg in ((1, "model warmup"), (2, "model warmup"), (3, "model warmup")):
            ax[1, j].text(0.5, 0.5, msg, ha="center", va="center", fontsize=12, color="gray")

    for row in ax:
        for a_ in row:
            a_.set_xticks([]); a_.set_yticks([])
    fig.suptitle(f"{transition(mp['src'], mc['src'])}   Δt={mc['gap_min']:.1f} min   "
                 f"#{mp['id']}→#{mc['id']}", fontsize=13, y=1.0)
    fig.tight_layout()
    fig.savefig(out_path, dpi=80, bbox_inches="tight")
    plt.close(fig)

    return dict(raw_mean=raw_mean, aff_mean=aff_mean, hp_mean=hp_mean, hp_blob=hp_area,
                a=round(a, 3), b=round(b, 1), masked_mean=masked_mean,
                masked_blob=mk_area, masked_frac=round(mk_frac, 4), std_mean=round(std_mean, 1))


def write_gallery(out: Path, rows: list[dict]):
    """One-at-a-time viewer: click image / arrow keys to walk through frames."""
    items = []
    for r in rows:
        bird_tag = " 🐦" if str(r.get("is_bird", "0")) == "1" else ""
        lbl_curr = r.get("label_curr", "") or ""
        lbl_prev = r.get("label_prev", "") or ""
        lbl_str = ""
        if lbl_curr == "bird": lbl_str = " &nbsp; <b style='color:#4f4'>BIRD(curr)</b>"
        elif lbl_prev == "bird": lbl_str = " &nbsp; <b style='color:#4f4'>BIRD(prev)</b>"
        elif lbl_curr == "ignore" or lbl_prev == "ignore": lbl_str = " &nbsp; <span style='color:#888'>[ignore]</span>"
        cap = (f"#{r['id_prev']}→#{r['id_curr']} &nbsp; <b>{html.escape(r['transition'])}</b> &nbsp; "
               f"Δt={r['gap_min']}min &nbsp; ({r['result_prev']}→{r['result_curr']}){lbl_str}{bird_tag}  |  "
               f"raw={float(r['raw_mean']):.1f} &nbsp; affine={float(r['aff_mean']):.1f} (a={r['a']}) &nbsp; "
               f"HP={float(r['hp_mean']):.1f} &nbsp; HP-model={float(r['masked_mean']):.1f} "
               f"blob={r['masked_blob']}px")
        items.append(dict(file=r["file"], cap=cap, tr=r["transition"],
                          res=r["result_curr"], bird=str(r.get("is_bird", "0"))))
    data_json = json.dumps(items)
    doc = f"""<!doctype html><meta charset=utf-8><title>pixel-delta viewer</title>
<style>
 html,body{{background:#111;color:#ddd;font:13px system-ui,sans-serif;margin:0;height:100%}}
 .bar{{padding:8px 12px;display:flex;gap:6px;align-items:center;flex-wrap:wrap;border-bottom:1px solid #333}}
 button{{background:#222;color:#ddd;border:1px solid #444;padding:5px 10px;border-radius:5px;cursor:pointer}}
 button.on{{background:#2a6;color:#000;border-color:#2a6}}
 #pos{{margin-left:auto;font-family:ui-monospace,monospace;color:#9c9}}
 #stage{{padding:10px 12px}}
 #img{{width:100%;max-width:1600px;display:block;cursor:pointer;user-select:none}}
 #stage.bird-active #img{{border:4px solid #e44;box-shadow:0 0 24px #e44;}}
 #cap{{margin-top:6px;color:#bbb;font-family:ui-monospace,monospace}}
 .hint{{color:#777;margin-left:6px}}
</style>
<div class=bar>
 <button id=prev>◀ Prev</button>
 <button id=next>Next ▶</button>
 <span class=hint>(click image or ← → keys)</span>
 &nbsp;|&nbsp; filter:
 <button data-f="tr:all" class=on>all</button>
 <button data-f="tr:RTC→RTC">RTC→RTC</button>
 <button data-f="tr:RTC→PIR">RTC→PIR</button>
 <button data-f="tr:PIR→PIR">PIR→PIR</button>
 <button data-f="tr:PIR→RTC">PIR→RTC</button>
 &nbsp;|&nbsp;
 <button data-f="res:process">process</button>
 <button data-f="res:clouds">clouds</button>
 &nbsp;|&nbsp;
 <button data-f="bird:1" style="background:#193;color:#cfc;border-color:#4a4">🐦 bird frames</button>
 <span id=pos></span>
</div>
<div id=stage>
 <img id=img>
 <div id=cap></div>
</div>
<script>
 const ALL = {data_json};
 let view = ALL.slice(), i = 0;
 const img=document.getElementById('img'), cap=document.getElementById('cap'), pos=document.getElementById('pos');
 function show(){{
   if(!view.length){{img.removeAttribute('src');cap.innerHTML='<em>(no frames match filter)</em>';pos.textContent='';return;}}
   i=(i%view.length+view.length)%view.length;
   const it=view[i]; img.src=it.file; cap.innerHTML=it.cap;
   document.getElementById('stage').classList.toggle('bird-active', it.bird==='1');
   pos.textContent=`${{i+1}} / ${{view.length}}`;
 }}
 const step=d=>{{i+=d;show();}};
 document.getElementById('next').onclick=()=>step(1);
 document.getElementById('prev').onclick=()=>step(-1);
 img.onclick=e=>step(e.offsetX < img.clientWidth/2 ? -1 : 1);
 document.addEventListener('keydown',e=>{{
   if(e.key==='ArrowRight'||e.key===' ')step(1);
   else if(e.key==='ArrowLeft')step(-1);
 }});
 document.querySelectorAll('button[data-f]').forEach(b=>b.onclick=()=>{{
   document.querySelectorAll('button[data-f]').forEach(x=>x.classList.remove('on')); b.classList.add('on');
   const [k,v]=b.dataset.f.split(':');
   if(v==='all') view=ALL.slice();
   else if(k==='tr') view=ALL.filter(it=>it.tr===v);
   else if(k==='res') view=ALL.filter(it=>it.res===v);
   else if(k==='bird') view=ALL.filter(it=>it.bird===v);
   i=0; show();
 }});
 show();
</script>"""
    (out / "gallery.html").write_text(doc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-frame", type=int, default=1560)
    ap.add_argument("--to-frame", type=int, default=None)
    ap.add_argument("--photo-server", default=os.getenv("PHOTO_SERVER", "http://192.168.1.110:8000"))
    ap.add_argument("--out", default=str(_here / "pixel_delta_out"))
    ap.add_argument("--width", type=int, default=800)
    ap.add_argument("--vmax", type=float, default=64.0)
    ap.add_argument("--blob-thr", type=float, default=25.0)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--gallery-only", action="store_true",
                    help="rebuild gallery.html from existing index.csv (no JPG fetch / re-render)")
    args = ap.parse_args()

    size = (args.width, int(args.width * 3 / 4))
    out = Path(args.out)
    out.mkdir(exist_ok=True)

    if args.gallery_only:
        rows = list(csv.DictReader(open(out / "index.csv")))
        write_gallery(out, rows)
        print(f"Rebuilt gallery.html from {len(rows)} rows in {out}")
        return

    s = Session()
    q = (s.query(BwFrame).filter(BwFrame.id >= args.from_frame)
         .filter(BwFrame.filename.isnot(None)))
    if args.to_frame:
        q = q.filter(BwFrame.id <= args.to_frame)
    frames = q.order_by(BwFrame.captured_at.asc()).all()
    print(f"{len(frames)} frames; {len(frames)-1} pairs at {size[0]}x{size[1]}  "
          f"(model: RTC-only EMA α={EMA_ALPHA}, warmup={MODEL_WARMUP})")

    model = dict(mean=None, hp_mean=None, hp_var=None, count=0, fast_left=N_FAST_FRAMES)
    rows, prev = [], None
    for f in frames:
        src = (f.meta or {}).get("source")
        meta = dict(id=f.id, src=src, result=f.result,
                    label=(f.meta or {}).get("label") or "",
                    t=f.captured_at.strftime("%m-%d %H:%M:%S"), ts=f.captured_at)
        try:
            lum = load_luma(f.filename, args.photo_server, size)
        except Exception as exc:
            print(f"  MISS #{f.id} {f.filename}: {exc}")
            prev = None
            continue

        if prev is not None:
            meta["gap_min"] = (meta["ts"] - prev[1]["ts"]).total_seconds() / 60.0
            seq = len(rows)
            label_curr = meta["label"]
            label_prev = prev[1]["label"]
            is_bird_curr = label_curr == "bird"
            is_bird_prev = label_prev == "bird"
            tr = transition(prev[1]["src"], src)
            # mark bird frames in filename so they're easy to spot on disk
            bird_tag = "_BIRD" if (is_bird_curr or is_bird_prev) else ""
            name = f"{seq:03d}_{tr}_{prev[1]['id']}-{f.id}{bird_tag}.png"
            model_snapshot = (dict(mean=model["mean"],
                                   hp_mean=model["hp_mean"],
                                   std=np.sqrt(np.maximum(model["hp_var"], 0)),
                                   count=model["count"],
                                   fast_left=model["fast_left"]) if model["count"] >= MODEL_WARMUP else None)
            m = render_pair(prev[0], lum, prev[1], meta, out / name, args.vmax,
                            args.blob_thr, model_snapshot)
            m.update(seq=seq, file=name, transition=tr,
                     id_prev=prev[1]["id"], id_curr=f.id, gap_min=round(meta["gap_min"], 2),
                     result_prev=prev[1]["result"], result_curr=f.result,
                     label_prev=label_prev, label_curr=label_curr,
                     is_bird=1 if (is_bird_curr or is_bird_prev) else 0)
            rows.append(m)
            print(f"  {name}  raw={m['raw_mean']:.1f} aff={m['aff_mean']:.1f} "
                  f"hp={m['hp_mean']:.1f} masked={m['masked_mean']:.1f}")
            if args.limit and len(rows) >= args.limit:
                break

        # RTC frames update the per-pixel background model (PIR never does).
        # mean tracks raw luma (panel 6 visual); hp_mean/hp_var track HP-space stats.
        # Scene-break detection: if the HP residual vs the existing model is very large
        # (camera repositioned), reset the model so it rebuilds from this frame.
        # After a reset, use FAST_ALPHA for N_FAST_FRAMES to converge in ~5-7 frames.
        if src == "rtc":
            hp_lum = high_pass(lum, HP_KERNEL)
            scene_break = False
            if model["mean"] is not None and model["count"] >= MODEL_WARMUP:
                hp_resid = float(np.abs(hp_lum - model["hp_mean"]).mean())
                if hp_resid > SCENE_BREAK_THR:
                    scene_break = True
                    model["mean"] = None   # reset — camera moved
                    print(f"  SCENE BREAK #{f.id} HP-resid={hp_resid:.1f} → model reset")
            if model["mean"] is None:
                model["mean"]       = lum.copy()
                model["hp_mean"]    = hp_lum.copy()
                model["hp_var"]     = np.zeros_like(lum)
                model["fast_left"]  = N_FAST_FRAMES
            else:
                alpha = FAST_ALPHA if model["fast_left"] > 0 else EMA_ALPHA
                if model["fast_left"] > 0:
                    model["fast_left"] -= 1
                d = lum - model["mean"]
                model["mean"] += alpha * d
                d_hp = hp_lum - model["hp_mean"]
                model["hp_mean"] += alpha * d_hp
                model["hp_var"]   = (1 - alpha) * (model["hp_var"] + alpha * d_hp * d_hp)
            model["count"] += 1
        prev = (lum, meta)

    if rows:
        cols = ["seq", "file", "transition", "id_prev", "id_curr", "gap_min",
                "result_prev", "result_curr", "label_prev", "label_curr", "is_bird",
                "raw_mean", "aff_mean", "a", "b",
                "hp_mean", "hp_blob", "masked_mean", "masked_blob", "masked_frac", "std_mean"]
        with open(out / "index.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k) for k in cols})
        write_gallery(out, rows)
        print(f"\nWrote {len(rows)} panels + index.csv + gallery.html to {out}")


if __name__ == "__main__":
    main()
