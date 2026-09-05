#!/usr/bin/env python3
"""
model_probe.py — is this scene separable at all?

Every detector so far has been built on a background model that was never
itself measured.  This asks the prior question, with no detector in the loop:

    given a frame, how well can we PREDICT what the background should look like?

If no reference can predict a held-out RTC frame to better than the bird
signal (~28-37 DN), then no threshold on top of it can work and the whole
EMA-background approach is wrong for this scene.  That is the real answer.

Three candidate references, compared head to head:

  prev   the most recent RTC frame before this one (~15-30 min stale).
         Sharp, but shadows have moved.
  ema    a single per-pixel exponential average — what the current pipeline
         uses.  Stable, but averages over hours, so it BLURS a scene whose
         shadows move faster than the cadence.
  knn    per-pixel MEDIAN of the k historical RTC frames most similar in
         lighting.  Non-parametric: no averaging over time, so it stays sharp,
         and the per-pixel MAD across those same neighbours gives a natural
         local sigma — the "what is normal here" estimate the EMA had to learn
         slowly and badly.

Metric A (detector-free): median |frame - affine-fitted reference| over
held-out RTC frames.  Compare against the ~28-37 DN bird signal.
Metric B: AUC separating labelled bird frames from RTC background frames.
0.5 = coin flip.

    python model_probe.py --width 320 --k 5 --n-rtc 400
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
    lo.write_bytes(d)
    return d


def load(fn, ps, size):
    im = Image.open(io.BytesIO(fetch(fn, ps))).convert("RGB").resize(size, Image.BILINEAR)
    return np.asarray(im, dtype=np.float32)


def gray(r):
    return r @ np.array([0.299, 0.587, 0.114], np.float32)


def desc(g):
    f = g.ravel()
    p25, p75 = np.percentile(f, [25, 75])
    hi, lo = f[f >= p75].mean(), f[f <= p25].mean()
    return np.array([f.mean() / 255, f.std() / 64, (hi / max(lo, 1)) / 8], np.float32)


def affine(ref, tgt):
    """tgt ~ a*ref + b, least squares — removes any global gain/offset so the
    residual measures STRUCTURE error, not exposure difference."""
    r = ref.ravel()
    A = np.vstack([r, np.ones_like(r)]).T
    (a, b), *_ = np.linalg.lstsq(A, tgt.ravel(), rcond=None)
    return a, b


def pred_err(ref_g, cur_g):
    a, b = affine(ref_g, cur_g)
    return float(np.median(np.abs(cur_g - (a * ref_g + b))))


def horprasert(rgb, E, sig):
    s2 = np.maximum(sig, 3.0) ** 2
    al = np.sum(rgb * E / s2, 2) / np.maximum(np.sum(E * E / s2, 2), 1e-6)
    res = (rgb - al[..., None] * E) / np.maximum(sig, 3.0)
    return al, np.sqrt(np.sum(res * res, 2))


def hf(g, k=5):
    return ndimage.uniform_filter(np.abs(g - ndimage.uniform_filter(g, k)), k)


def response(rgb, E, sig, k_bg=25, occ_thr=0.35, hf_min=2.0):
    """Detector response: chroma distortion and local darkening, plus the
    structure-occlusion gate (under a shadow the background's fine detail
    survives scaled by illumination; under an opaque bird it vanishes)."""
    al, cd = horprasert(rgb, E, sig)
    al_bg = ndimage.uniform_filter(al, k_bg)
    ad = np.maximum(0.0, al_bg - al)
    cd_s = ndimage.uniform_filter(cd, 3)
    ad_s = ndimage.uniform_filter(ad, 3)
    hE, hI = hf(gray(E)), hf(gray(rgb))
    occ = np.clip(1.0 - hI / (np.maximum(al_bg, 0.05) * hE + 1e-3), 0, 1)
    ok = (hE <= hf_min) | (occ > occ_thr)
    ado = ndimage.uniform_filter(np.where(ok, ad, 0.0), 3)

    # Frame-max throws away every spatial cue: it is set by the single worst
    # background artifact anywhere in 76800 pixels.  A bird is a COMPACT object
    # of a known rough size, so score the best blob inside a bird-plausible
    # area band instead — this is the statistic the reference deserves.
    def blob_score(field, lo, hi):
        best = 0.0
        for thr in np.percentile(field, (99.0, 99.5, 99.9)):
            lbl, n = ndimage.label(field > thr, structure=np.ones((3, 3), bool))
            if n == 0:
                continue
            cnt = np.bincount(lbl.ravel()); cnt[0] = 0
            for i in np.flatnonzero((cnt >= lo) & (cnt <= hi)):
                best = max(best, float(field[lbl == i].mean()))
        return best

    return dict(cd_p999=float(np.percentile(cd_s, 99.9)),
                cd_max=float(cd_s.max()),
                ad_p999=float(np.percentile(ad_s, 99.9)),
                ad_max=float(ad_s.max()),
                adocc_p999=float(np.percentile(ado, 99.9)),
                adocc_max=float(ado.max()),
                cd_blob=blob_score(cd_s, 15, 800),
                ad_blob=blob_score(ad_s, 15, 800))


def auc(pos, neg):
    pos, neg = np.asarray(pos, float), np.asarray(neg, float)
    if not len(pos) or not len(neg):
        return float("nan")
    d = np.subtract.outer(pos, neg)
    return float((d > 0).mean() + 0.5 * (d == 0).mean())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--photo-server", default=os.getenv("PHOTO_SERVER", "http://192.168.1.110:8000"))
    ap.add_argument("--width", type=int, default=320)
    ap.add_argument("--k", type=int, default=5, help="neighbours for the knn reference")
    ap.add_argument("--n-rtc", type=int, default=400, help="RTC frames to evaluate as negatives")
    ap.add_argument("--from-frame", type=int, default=500)
    ap.add_argument("--ema-alpha", type=float, default=0.15)
    args = ap.parse_args()
    size = (args.width, int(args.width * 3 / 4))

    s = Session()
    frames = (s.query(BwFrame).filter(BwFrame.id >= args.from_frame)
              .filter(BwFrame.filename.isnot(None))
              .order_by(BwFrame.captured_at.asc()).all())
    rows = [(f.id, f.filename, f.captured_at, (f.meta or {}).get("source"),
             (f.meta or {}).get("label") or "") for f in frames]
    rows = [r for r in rows if r[4] not in IGNORE]
    rtc = [r for r in rows if r[3] == "rtc"]
    birds = [r for r in rows if r[4] == "bird"]
    print(f"{len(rows)} frames | {len(rtc)} RTC pool | {len(birds)} labelled birds")

    # ── pass A: descriptor for every RTC frame (small decode, lighting only)
    print("indexing RTC pool ...")
    small = (160, 120)
    pool = []
    for i, (fid, fn, t, _, _) in enumerate(rtc):
        try:
            pool.append((fid, fn, t, desc(gray(load(fn, args.photo_server, small)))))
        except Exception:
            continue
        if i % 400 == 0:
            print(f"  {i}/{len(rtc)}")
    print(f"  pool = {len(pool)}")
    P_t = np.array([p[2].timestamp() for p in pool])
    P_d = np.stack([p[3] for p in pool])

    # ── build the EMA reference once, in time order (what the pipeline uses)
    print("building EMA reference ...")
    ema = None
    for fid, fn, t, _ in pool[::3]:                    # every 3rd frame is plenty
        try:
            r = load(fn, args.photo_server, size)
        except Exception:
            continue
        ema = r.copy() if ema is None else ema + args.ema_alpha * (r - ema)

    # ── evaluation set: every bird + a spread of RTC negatives
    step = max(1, len(pool) // args.n_rtc)
    ev = [("rtc", p[0], p[1], p[2]) for p in pool[::step]] + \
         [("bird", b[0], b[1], b[2]) for b in birds]
    print(f"evaluating {len(ev)} frames ({sum(1 for e in ev if e[0]=='bird')} birds) ...")

    KEYS = ("prev", "ema", "knn", "prev+mad")
    out = {k: {"rtc": [], "bird": []} for k in KEYS}
    resp = {k: {"rtc": [], "bird": []} for k in KEYS}
    for n, (kind, fid, fn, t) in enumerate(ev):
        try:
            cur = load(fn, args.photo_server, size)
        except Exception:
            continue
        cg = gray(cur)
        ts = t.timestamp()

        # prev: nearest RTC frame strictly before, at least 60s back (not itself)
        idx = np.where(P_t < ts - 60)[0]
        prev_rgb = None
        if len(idx):
            j = idx[np.argmax(P_t[idx])]
            try:
                prev_rgb = load(pool[j][1], args.photo_server, size)
                out["prev"][kind].append(pred_err(gray(prev_rgb), cg))
                resp["prev"][kind].append(response(cur, prev_rgb, np.full_like(prev_rgb, 8.0)))
            except Exception:
                prev_rgb = None

        if ema is not None:
            out["ema"][kind].append(pred_err(gray(ema), cg))
            resp["ema"][kind].append(response(cur, ema, np.full_like(ema, 8.0)))

        # knn: k most lighting-similar PAST RTC frames -> per-pixel median + MAD
        if len(idx) >= args.k:
            d = np.linalg.norm(P_d[idx] - desc(cg), axis=1)
            pick = idx[np.argsort(d)[:args.k]]
            try:
                st = np.stack([load(pool[j][1], args.photo_server, size) for j in pick])
                med = np.median(st, 0)
                mad = np.median(np.abs(st - med), 0) * 1.4826
                out["knn"][kind].append(pred_err(gray(med), cg))
                resp["knn"][kind].append(response(cur, med, np.maximum(mad, 3.0)))
                if prev_rgb is not None:
                    out["prev+mad"][kind].append(pred_err(gray(prev_rgb), cg))
                    resp["prev+mad"][kind].append(
                        response(cur, prev_rgb, np.maximum(mad, 3.0)))
            except Exception:
                pass
        if n % 100 == 0:
            print(f"  {n}/{len(ev)}")

    print("\n=== METRIC A — background prediction error (detector-free) ===")
    print("median |frame - affine-fitted reference|, DN.  Bird signal is ~28-37 DN.")
    print(f"{'reference':>8} | {'RTC p50':>8} {'RTC p90':>8} | {'bird p50':>9}")
    for k in KEYS:
        a = np.array(out[k]["rtc"]); b = np.array(out[k]["bird"])
        if not len(a):
            continue
        print(f"{k:>8} | {np.percentile(a,50):8.1f} {np.percentile(a,90):8.1f} | "
              f"{(np.percentile(b,50) if len(b) else float('nan')):9.1f}")

    print("\n=== METRIC B — separability, AUC bird vs RTC (0.5 = coin flip) ===")
    STATS = ("cd_max", "ad_max", "adocc_max", "cd_blob", "ad_blob")
    print(f"{'reference':>9} | " + " ".join(f"{s:>10}" for s in STATS))
    for k in KEYS:
        if not resp[k]["rtc"] or not resp[k]["bird"]:
            continue
        cells = [f"{auc([r[s] for r in resp[k]['bird']], [r[s] for r in resp[k]['rtc']]):10.3f}"
                 for s in STATS]
        print(f"{k:>9} | " + " ".join(cells))

    # AUC hides WHERE the overlap is.  Detection is a tail problem: the bird's
    # peak response has to beat the worst background artifact in the frame, so
    # print both distributions and see how far they actually sit apart.
    print("\n=== overlap: peak response per frame, prev reference ===")
    for s in ("cd_max", "ad_max"):
        a = np.array([r[s] for r in resp["prev"]["rtc"]])
        b = np.array([r[s] for r in resp["prev"]["bird"]])
        print(f"  {s:>8}  RTC  p50={np.percentile(a,50):7.2f} p90={np.percentile(a,90):7.2f} "
              f"p99={np.percentile(a,99):7.2f}")
        print(f"  {'':>8}  BIRD p50={np.percentile(b,50):7.2f} p90={np.percentile(b,90):7.2f} "
              f"max={b.max():7.2f}")
        thr = np.percentile(a, 90)      # allow 10% of background frames to fire
        print(f"  {'':>8}  -> at a threshold passing 10% of RTC frames, "
              f"bird recall = {100*(b>thr).mean():.0f}%")
        thr = np.percentile(a, 99)
        print(f"  {'':>8}  -> at a threshold passing  1% of RTC frames, "
              f"bird recall = {100*(b>thr).mean():.0f}%")


if __name__ == "__main__":
    main()
