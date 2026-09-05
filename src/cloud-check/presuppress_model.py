#!/usr/bin/env python3
"""
presuppress_model.py — how much CAN be suppressed on-device, honestly?

The hand-tuned OR-rule reached 17.6% at 0/68 birds lost.  Two questions:

  1. How risky is "0 of 68" really?  Zero observed losses does NOT mean zero
     loss rate.  By the rule of three, the 95% upper bound on the true loss
     rate is 3/68 = 4.4% — so a rule that looks perfect here could still miss
     roughly 1 bird in 23 in the field.  Any threshold placed exactly at the
     most extreme of 68 samples is fitted to noise.

  2. Can a learned combination beat hand-tuned thresholds?  Evidence should
     ACCUMULATE — burst position 3 plus a short quiet plus midday plus no local
     residual is far safer to suppress than any single condition crossing a
     bound.

Everything is scored with GroupKFold BY DATE.  Frames from one day are highly
correlated (same weather, and a bird visit spans several frames), so a random
split would leak and report a fantasy number.

All features are ESP32-computable: the RTC gives time and quiet-interval, and
the tile grid gives the illumination fit against the previous frame.

    python presuppress_model.py
"""
from __future__ import annotations

import io, sys
from pathlib import Path

import numpy as np
from PIL import Image
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.model_selection import GroupKFold

sys.path.insert(0, "/workspace/src/python_bw_src")
from db import engine          # noqa: E402
from sqlalchemy import text    # noqa: E402

CACHE = Path("/workspace/src/cloud-check/.jpg_cache")
FEAT_NPZ = Path("/workspace/src/cloud-check/presuppress_feats.npz")


def tile_y(fn):
    p = CACHE / fn
    if not p.exists():
        return None
    im = Image.open(io.BytesIO(p.read_bytes())).convert("YCbCr").resize((160, 120),
                                                                        Image.BILINEAR)
    return np.asarray(im, np.float32).reshape(15, 8, 20, 8, 3).mean((1, 3))


def build():
    with engine.connect() as c:
        rows = c.execute(text("""select id, captured_at, filename, meta->>'source' src,
            coalesce(meta->>'label','') lbl from bw_frames
            where filename is not null order by captured_at asc""")).all()

    out, prev, prev_t = [], None, None
    gm_hist = []                      # recent global means -> "is the light unstable?"
    last_pir, burst = None, 0
    for r in rows:
        t = tile_y(r.filename)
        if t is None:
            continue
        y = t[..., 0]
        gm = float(y.mean())
        if prev is not None:
            dt = (r.captured_at - prev_t).total_seconds()
            f = prev[..., 0].ravel()
            A = np.vstack([f, np.ones_like(f)]).T
            (a, b), *_ = np.linalg.lstsq(A, y.ravel(), rcond=None)
            res = np.abs(y - (a * prev[..., 0] + b))
            du = (t[..., 1] - prev[..., 1]); du -= np.median(du)
            dv = (t[..., 2] - prev[..., 2]); dv -= np.median(dv)
            if r.src == "pir" and r.lbl not in ("ignore", "delete"):
                gap = 1e6 if last_pir is None else (r.captured_at - last_pir).total_seconds()
                burst = burst + 1 if gap < 60 else 0
                # instability of the light over the recent past (device can keep
                # a tiny ring buffer of global means in NVS)
                inst = float(np.std(gm_hist[-8:])) if len(gm_hist) >= 3 else 0.0
                hour = r.captured_at.hour + r.captured_at.minute / 60
                out.append(dict(
                    id=r.id, date=str(r.captured_at.date()),
                    bird=int(r.lbl == "bird"),
                    log_gap=float(np.log10(max(gap, 1.0))), burst=burst,
                    hour=hour, sin_h=np.sin(2*np.pi*hour/24), cos_h=np.cos(2*np.pi*hour/24),
                    dt=dt, gm=gm, instab=inst,
                    gm_diff=abs(gm - float(prev[..., 0].mean())),
                    slope=abs(a - 1.0), offset=abs(b),
                    n_changed=int((np.abs(y - prev[..., 0]) > 12).sum()),
                    res_max=float(res.max()), res_p95=float(np.percentile(res, 95)),
                    n_res=int((res > 15).sum()),
                    chroma=float(np.sqrt(du*du + dv*dv).max()),
                ))
                last_pir = r.captured_at
        gm_hist.append(gm)
        prev, prev_t = t, r.captured_at
    return out


def main():
    recs = build()
    keys = ["log_gap", "burst", "sin_h", "cos_h", "hour", "instab", "gm", "gm_diff",
            "slope", "offset", "n_changed", "res_max", "res_p95", "n_res", "chroma"]
    X = np.array([[r[k] for k in keys] for r in recs], float)
    y = np.array([r["bird"] for r in recs])
    g = np.array([r["date"] for r in recs])
    print(f"{len(X)} PIR events, {y.sum()} birds, {len(set(g))} distinct days")
    print(f"rule of three: 0 losses in {y.sum()} birds => true loss rate could still "
          f"be up to {300/y.sum():.1f}% (95% upper bound)\n")

    # out-of-fold probabilities, grouped by DAY so nothing leaks
    oof = np.zeros(len(X))
    for tr, te in GroupKFold(n_splits=5).split(X, y, groups=g):
        m = HistGradientBoostingClassifier(max_depth=3, max_iter=250,
                                           learning_rate=0.06, random_state=0)
        m.fit(X[tr], y[tr])
        oof[te] = m.predict_proba(X[te])[:, 1]

    print("Cross-validated (grouped by day) — suppress the lowest-scoring frames:")
    print(f"{'suppressed':>11} | {'birds lost':>11} | {'of birds':>9}")
    order = np.argsort(oof)
    for frac in (0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70):
        n = int(frac * len(order))
        lost = int(y[order[:n]].sum())
        print(f"{100*frac:9.0f}% | {lost:>4}/{y.sum():<6} | {100*lost/y.sum():8.1f}%")

    print("\nLargest suppression with ZERO birds lost, out-of-fold:")
    best = 0
    for n in range(len(order)):
        if y[order[:n+1]].sum() > 0:
            best = n; break
    print(f"  {best}/{len(order)} = {100*best/len(order):.1f}%")

    m = HistGradientBoostingClassifier(max_depth=3, max_iter=250, learning_rate=0.06,
                                       random_state=0).fit(X, y)
    from sklearn.inspection import permutation_importance
    imp = permutation_importance(m, X, y, n_repeats=5, random_state=0, scoring="average_precision")
    print("\nfeature importance (permutation, average precision):")
    for i in np.argsort(-imp.importances_mean)[:8]:
        print(f"  {keys[i]:>10} {imp.importances_mean[i]:+.4f}")


if __name__ == "__main__":
    main()
