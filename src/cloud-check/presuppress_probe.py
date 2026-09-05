#!/usr/bin/env python3
"""
presuppress_probe.py — can the ESP32 still earn its keep, by the INVERSE rule?

Detecting a bird on-device failed: a bird is ~100 anomalous pixels in 480k, so
the signal is smaller than the noise.  This asks the opposite question, which is
a far better-posed one for a microcontroller:

    can we recognise that a PIR trigger was EXPLAINED BY A LIGHTING CHANGE,
    and suppress only then?

That is a large, global, low-frequency signal — exactly what cheap global
statistics capture.  And it is suppression-only, so it cannot cost recall the
way a detector does: a pigeon does not change the whole frame.

The rule has two halves, and both must hold to suppress:
  1. the scene really did change globally   (otherwise why suppress at all)
  2. after removing a global illumination fit `a*prev + b`, NOTHING LOCAL
     remains (a bird would leave a compact residual; a cloud would not)

All features are computed here the way new firmware would: from a coarse tile
grid over the frame, against the previous captured frame only — no background
model, no history, nothing that needs storing beyond one small tile array.
Device telemetry is deliberately NOT used: it comes from the old firmware,
across months of changing ETTR/AWB behaviour, so it is not a consistent basis.

    python presuppress_probe.py --grid 20x15
"""
from __future__ import annotations

import argparse, io, os, sys, urllib.request
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

_here = Path(__file__).parent
sys.path.insert(0, str(_here)); sys.path.insert(0, str(_here.parent / "python_bw_src"))
from dotenv import load_dotenv
load_dotenv(_here.parent / "python_bw_src" / ".env")
from db import BwFrame, Session   # noqa: E402

CACHE = _here / ".jpg_cache"
IGNORE = {"ignore", "delete"}


def fetch(fn, ps):
    CACHE.mkdir(exist_ok=True)
    lo = CACHE / fn
    if lo.exists():
        return lo.read_bytes()
    d = urllib.request.urlopen(f"{ps.rstrip('/')}/static/{fn}", timeout=20).read()
    lo.write_bytes(d); return d


def tiles(fn, ps, gw, gh):
    """Tile means in Y, U, V — what the firmware can compute during JPEG decode."""
    im = Image.open(io.BytesIO(fetch(fn, ps))).convert("YCbCr").resize((gw * 8, gh * 8),
                                                                      Image.BILINEAR)
    a = np.asarray(im, np.float32)
    a = a.reshape(gh, 8, gw, 8, 3).mean((1, 3))
    return a[..., 0], a[..., 1], a[..., 2]


def feats(y, u, v, py, pu, pv):
    """Everything the inverse rule needs, from two consecutive tile grids."""
    f = py.ravel()
    A = np.vstack([f, np.ones_like(f)]).T
    (a, b), *_ = np.linalg.lstsq(A, y.ravel(), rcond=None)
    pred = a * py + b
    res = np.abs(y - pred)
    # chroma residual, global cast removed (AWB re-balance is not a bird)
    du = (u - pu) - np.median(u - pu)
    dv = (v - pv) - np.median(v - pv)
    chroma = np.sqrt(du * du + dv * dv)
    # compactness: largest run of adjacent high-residual tiles.  A bird is a
    # small tight cluster; diffuse cloud residual is scattered or absent.
    hot = res > max(8.0, 3.0 * np.median(res))
    lbl, n = ndimage.label(hot, structure=np.ones((3, 3), bool))
    big = int(np.bincount(lbl.ravel())[1:].max()) if n else 0
    return dict(
        gm_diff=float(abs(y.mean() - py.mean())),          # global brightness move
        slope=float(abs(a - 1.0)), offset=float(abs(b)),   # global illumination fit
        n_changed=int((np.abs(y - py) > 12).sum()),        # how much of the frame moved
        res_max=float(res.max()), res_p95=float(np.percentile(res, 95)),
        res_med=float(np.median(res)),
        n_res=int((res > 15).sum()),                       # tiles unexplained by the fit
        blob=big,                                          # compactness of the unexplained part
        chroma_max=float(chroma.max()),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--photo-server", default=os.getenv("PHOTO_SERVER", "http://192.168.1.110:8000"))
    ap.add_argument("--grid", default="20x15")
    ap.add_argument("--max-dt", type=float, default=600.0,
                    help="only trust a comparison against a predecessor this recent (s)")
    args = ap.parse_args()
    gw, gh = (int(x) for x in args.grid.split("x"))

    s = Session()
    fr = s.query(BwFrame).filter(BwFrame.filename.isnot(None)).order_by(
        BwFrame.captured_at.asc()).all()
    print(f"{len(fr)} frames, grid {gw}x{gh}")

    recs, prev = [], None
    for i, f in enumerate(fr):
        meta = f.meta or {}
        try:
            y, u, v = tiles(f.filename, args.photo_server, gw, gh)
        except Exception:
            continue
        if prev is not None:
            dt = (f.captured_at - prev[3]).total_seconds()
            d = feats(y, u, v, *prev[:3])
            d.update(id=f.id, dt=dt, src=meta.get("source"),
                     bird=(meta.get("label") == "bird"),
                     ign=(meta.get("label") or "") in IGNORE)
            recs.append(d)
        prev = (y, u, v, f.captured_at)
        if i % 500 == 0:
            print(f"  {i}/{len(fr)}")

    ok = [x for x in recs if x["dt"] <= args.max_dt]
    pir = [x for x in ok if x["src"] == "pir" and not x["ign"]]
    bird = [x for x in ok if x["bird"]]
    print(f"\nusable (predecessor within {args.max_dt:.0f}s): {len(ok)}  "
          f"PIR {len(pir)}  birds {len(bird)}")

    print("\n--- single features: suppress above the most extreme bird (100% recall) ---")
    for k in ("gm_diff", "n_changed", "slope", "offset", "res_max", "res_p95", "n_res",
              "blob", "chroma_max"):
        bv = np.array([x[k] for x in bird], float)
        pv = np.array([x[k] for x in pir], float)
        n_hi = int((pv > bv.max()).sum())
        n_lo = int((pv < bv.min()).sum())
        print(f"  {k:>10}  bird {bv.min():7.2f}..{bv.max():7.2f} | "
              f"suppress-above {100*n_hi/len(pv):5.1f}%  suppress-below {100*n_lo/len(pv):5.1f}%")

    print("\n--- the actual rule: scene moved globally AND nothing local remains ---")
    print("    suppress if (n_changed >= C) and (res_p95 <= R) and (blob <= B)")
    best = []
    for C in (0, 20, 40, 80, 120, 160, 200):
        for R in (4, 6, 8, 10, 12, 15, 20, 25):
            for B in (0, 1, 2, 3, 5):
                bs = sum(1 for x in bird if x["n_changed"] >= C and x["res_p95"] <= R and x["blob"] <= B)
                ps = sum(1 for x in pir if x["n_changed"] >= C and x["res_p95"] <= R and x["blob"] <= B)
                if bs == 0:
                    best.append((ps / len(pir), C, R, B, ps))
    best.sort(reverse=True)
    print(f"    {'suppressed':>10} | C(n_changed>=) R(res_p95<=) B(blob<=)")
    for s_, C, R, B, ps in best[:10]:
        print(f"    {100*s_:9.1f}% | {C:>14} {R:>12} {B:>9}   ({ps}/{len(pir)} PIR)")
    if not best:
        print("    no zero-bird-loss region found")


if __name__ == "__main__":
    main()
