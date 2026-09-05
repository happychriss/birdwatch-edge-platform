#!/usr/bin/env python3
"""
group_probe.py — how should RTC frames be grouped, and how many groups?

The regime bank tried to group frames by a 3-number APPEARANCE descriptor and
failed: 79% of frames landed in one cluster, so the bank degenerated to a
single blurry EMA.  This tests a better-founded idea.

Shadow POSITION is not a property of the picture — it is a deterministic
function of where the sun is.  Two frames taken at the same solar elevation and
azimuth have their shadows in the same place, even months apart.  Cloud cover
then sets the shadow CONTRAST.  So the natural grouping variables are
(solar elevation, solar azimuth, cloud state) — two of which are computable
exactly from the timestamp, with no reference to the image at all.

Each scheme is scored by the same detector-free metric used in model_probe.py:
build a per-group model from TRAIN frames, then measure how well it predicts
HELD-OUT frames.  Sweeping K gives the elbow — the answer to "how many groups".

Baseline to beat: the previous RTC frame, which needs no model at all.

    python group_probe.py --width 240 --k-list 1,2,4,8,16,32,64,128
"""
from __future__ import annotations

import argparse, io, math, os, sys, urllib.request
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from PIL import Image
from scipy.cluster.vq import kmeans2

_here = Path(__file__).parent
sys.path.insert(0, str(_here)); sys.path.insert(0, str(_here.parent / "python_bw_src"))
from dotenv import load_dotenv
load_dotenv(_here.parent / "python_bw_src" / ".env")
from db import BwFrame, Session   # noqa: E402

CACHE = _here / ".jpg_cache"
IGNORE = {"ignore", "delete"}
LAT, LON = 51.5, 10.0                 # BW_GEO_LAT_DEG / BW_GEO_LON_DEG
BERLIN = ZoneInfo("Europe/Berlin")    # DS3231 runs Berlin local time (CET/CEST)


def solar_pos(dt_local):
    """NOAA solar position -> (elevation deg, azimuth deg). Input is Berlin local."""
    dt = dt_local.replace(tzinfo=BERLIN).astimezone(timezone.utc)
    day = dt.timetuple().tm_yday
    hour = dt.hour + dt.minute / 60 + dt.second / 3600
    g = 2 * math.pi / 365 * (day - 1 + (hour - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    tst = hour * 60 + eqtime + 4 * LON
    ha = math.radians(tst / 4 - 180)
    lat_r = math.radians(LAT)
    cz = (math.sin(lat_r) * math.sin(decl)
          + math.cos(lat_r) * math.cos(decl) * math.cos(ha))
    zen = math.acos(max(-1.0, min(1.0, cz)))
    elev = 90 - math.degrees(zen)
    sz = math.sin(zen)
    if abs(sz) < 1e-6:
        return elev, 180.0
    ca = -(math.sin(lat_r) * math.cos(zen) - math.sin(decl)) / (math.cos(lat_r) * sz)
    az = math.degrees(math.acos(max(-1.0, min(1.0, ca))))
    return elev, (360 - az if ha > 0 else az)


def fetch(fn, ps):
    CACHE.mkdir(exist_ok=True)
    lo = CACHE / fn
    if lo.exists():
        return lo.read_bytes()
    d = urllib.request.urlopen(f"{ps.rstrip('/')}/static/{fn}", timeout=20).read()
    lo.write_bytes(d); return d


def load(fn, ps, size):
    im = Image.open(io.BytesIO(fetch(fn, ps))).convert("RGB").resize(size, Image.BILINEAR)
    return np.asarray(im, dtype=np.uint8)


def gray(r):
    return r.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)


def cloud_desc(g):
    """Dynamic range: sunny = bright sky over dark shadow (high); overcast flat."""
    f = g.ravel()
    p25, p75 = np.percentile(f, [25, 75])
    return float(f[f >= p75].mean() / max(f[f <= p25].mean(), 1.0))


def pred_err(ref_g, cur_g):
    r = ref_g.ravel()
    A = np.vstack([r, np.ones_like(r)]).T
    (a, b), *_ = np.linalg.lstsq(A, cur_g.ravel(), rcond=None)
    return float(np.median(np.abs(cur_g - (a * ref_g + b))))


def evaluate(labels_tr, labels_te, imgs_tr, gray_te, K, min_members=3, global_med=None):
    """Per-group median from TRAIN frames, scored on HELD-OUT frames."""
    med = {}
    for c in range(K):
        m = np.flatnonzero(labels_tr == c)
        if len(m) >= min_members:
            med[c] = np.median(imgs_tr[m].astype(np.float32), 0)
    errs, fell_back = [], 0
    for i, c in enumerate(labels_te):
        ref = med.get(int(c))
        if ref is None:
            ref, fell_back = global_med, fell_back + 1
        errs.append(pred_err(gray(ref.astype(np.uint8)), gray_te[i]))
    return float(np.median(errs)), fell_back


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--photo-server", default=os.getenv("PHOTO_SERVER", "http://192.168.1.110:8000"))
    ap.add_argument("--width", type=int, default=240)
    ap.add_argument("--from-frame", type=int, default=500)
    ap.add_argument("--k-list", default="1,2,4,8,16,32,64,128")
    ap.add_argument("--test-frac", type=float, default=0.2)
    ap.add_argument("--night-mean", type=float, default=60.0)
    args = ap.parse_args()
    size = (args.width, int(args.width * 3 / 4))
    Ks = [int(x) for x in args.k_list.split(",")]

    s = Session()
    fr = [f for f in s.query(BwFrame).filter(BwFrame.id >= args.from_frame)
          .filter(BwFrame.filename.isnot(None))
          .order_by(BwFrame.captured_at.asc()).all()
          if (f.meta or {}).get("source") == "rtc"
          and ((f.meta or {}).get("label") or "") not in IGNORE]
    print(f"{len(fr)} RTC frames; loading at {size} ...")

    imgs, feats, times = [], [], []
    for i, f in enumerate(fr):
        try:
            im = load(f.filename, args.photo_server, size)
        except Exception:
            continue
        g = gray(im)
        if g.mean() < args.night_mean:
            continue
        el, az = solar_pos(f.captured_at)
        imgs.append(im)
        feats.append((el, az, cloud_desc(g), f.captured_at.hour + f.captured_at.minute / 60))
        times.append(f.captured_at)
        if i % 500 == 0:
            print(f"  {i}/{len(fr)}")
    imgs = np.stack(imgs); F = np.array(feats, np.float32)
    print(f"  usable {len(imgs)} frames, {imgs.nbytes/1e6:.0f} MB")
    print(f"  solar elevation {F[:,0].min():.1f}..{F[:,0].max():.1f} deg, "
          f"azimuth {F[:,1].min():.0f}..{F[:,1].max():.0f} deg, "
          f"cloud ratio {F[:,2].min():.2f}..{F[:,2].max():.2f}")

    rng = np.random.default_rng(0)
    te = rng.random(len(imgs)) < args.test_frac
    tr = ~te
    gray_te = [gray(imgs[i]) for i in np.flatnonzero(te)]
    global_med = np.median(imgs[tr].astype(np.float32), 0)
    print(f"  train {tr.sum()}  test {te.sum()}")

    # ── baseline 1: the previous RTC frame (no model at all)
    prev_err = []
    for i in np.flatnonzero(te):
        if i == 0:
            continue
        prev_err.append(pred_err(gray(imgs[i - 1]), gray(imgs[i])))
    print(f"\nBASELINE  previous RTC frame : {np.median(prev_err):5.2f} DN")
    print(f"BASELINE  global median (K=1): "
          f"{np.median([pred_err(gray(global_med.astype(np.uint8)), g) for g in gray_te]):5.2f} DN")

    # ── feature spaces to cluster on
    def norm(x):
        return (x - x.mean(0)) / (x.std(0) + 1e-6)
    small = np.stack([gray(im)[::4, ::4].ravel() for im in imgs])
    small = small - small.mean(0)
    U, S, Vt = np.linalg.svd(small, full_matrices=False)
    pca = U[:, :8] * S[:8]
    spaces = {
        "clock (hour)":        norm(F[:, 3:4]),
        "solar (elev,azim)":   norm(F[:, 0:2]),
        "solar + cloud":       norm(F[:, [0, 1, 2]]),
        "appearance (PCA8)":   norm(pca),
    }

    # Clustering into many groups approaches its own limit: just RETRIEVE the
    # most similar train frames per query.  This is the floor any grouping
    # scheme can reach, so it bounds the whole idea.
    print("\nRETRIEVAL (limit of 'more groups') — per-pixel median of the n most")
    print("similar TRAIN frames, by feature space:")
    tr_i = np.flatnonzero(tr); te_i = np.flatnonzero(te)
    for fname, X in (("solar+cloud", norm(F[:, [0, 1, 2]])), ("appearance PCA8", None)):
        pass
    for fname in ("solar + cloud", "appearance (PCA8)"):
        X = {"solar + cloud": norm(F[:, [0, 1, 2]]),
             "appearance (PCA8)": norm(pca)}[fname]
        for nn in (1, 3, 5, 9, 15):
            errs = []
            for i in te_i:
                d = np.linalg.norm(X[tr_i] - X[i], axis=1)
                pick = tr_i[np.argsort(d)[:nn]]
                ref = (imgs[pick[0]].astype(np.float32) if nn == 1
                       else np.median(imgs[pick].astype(np.float32), 0))
                errs.append(pred_err(gray(ref.astype(np.uint8)), gray(imgs[i])))
            print(f"  {fname:>20}  n={nn:<3} {np.median(errs):5.2f} DN")

    # Is appearance retrieval genuinely matching LIGHTING, or is it just finding
    # the temporally adjacent frame?  And deployment is causal: at a PIR event
    # only PAST RTC frames exist.  Both questions decide how to read the result.
    T = np.array([t_.timestamp() for t_ in times])
    Xa = norm(pca)
    for label, causal in (("any frame", False), ("PAST frames only (deployable)", True)):
        errs, gaps = [], []
        for i in te_i:
            cand = tr_i[T[tr_i] < T[i] - 60] if causal else tr_i
            if not len(cand):
                continue
            d = np.linalg.norm(Xa[cand] - Xa[i], axis=1)
            j = cand[int(np.argmin(d))]
            errs.append(pred_err(gray(imgs[j]), gray(imgs[i])))
            gaps.append(abs(T[i] - T[j]) / 3600.0)
        g = np.array(gaps)
        print(f"\n  retrieval n=1, {label}: {np.median(errs):5.2f} DN")
        print(f"    time gap to the retrieved frame: median {np.median(g):6.2f} h, "
              f"p90 {np.percentile(g,90):7.2f} h")
        print(f"    share of matches within 1h: {100*(g<1).mean():4.1f}%  "
              f"within 24h: {100*(g<24).mean():4.1f}%  beyond a week: {100*(g>168).mean():4.1f}%")

    print(f"\n{'scheme':>20} | " + " ".join(f"K={k:<5}" for k in Ks))
    print(f"{'':>20} | " + " ".join(f"{'':7}" for _ in Ks))
    for name, X in spaces.items():
        cells = []
        for K in Ks:
            if K > tr.sum() // 3:
                cells.append("   -   "); continue
            cen, lab = kmeans2(X, K, minit="++", seed=0, iter=30)
            e, fb = evaluate(lab[tr], lab[te], imgs[tr], gray_te, K, global_med=global_med)
            cells.append(f"{e:6.2f} ")
        print(f"{name:>20} | " + " ".join(cells))


if __name__ == "__main__":
    main()
