#!/usr/bin/env python3
"""
presuppress_model.py — fit the on-device suppression rule and export it as C.

The rule answers the INVERSE question: not "is there a bird?" (measured
impossible on this scene — best background-subtraction reached AUC 0.712 and
32% recall at a 10% false-positive threshold) but "was this PIR trigger already
EXPLAINED by the time of day and the recent trigger pattern?"

Measured ablation, cross-validated by day, birds lost per suppression level:

    feature set                         20%  30%  40%  50%   zero-loss max
    image only (no clock)                 6    7    9   14        5.2%
    clock + prev-frame residual           3    5    7   10       13.5%
    clock hour only                       2    3    3    7       15.4%
    solar elev + quiet gap + burst        0    1    4    5       22.9%

Adding pixels made it WORSE at every operating point — the image features were
fitting noise.  So this file reads no images at all: three numbers from the RTC.

Solar elevation rather than clock hour because it is seasonally transferable —
"12 degrees above the horizon" means the same in January as in June, while
"07:00" does not.  All training data is May-September, so this matters.

The ESP32 cannot run a gradient-boosted model, so the fitted rule is discretised
into a small lookup table (elevation x quiet-gap x burst-position) emitted as
presuppress_table.h.  The table is scored under the same GroupKFold-by-date
split as the model it replaces: if it does not match, do not flash.

    python presuppress_model.py                 # evaluate
    python presuppress_model.py --export        # also write presuppress_table.h
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.model_selection import GroupKFold

_here = Path(__file__).parent
sys.path.insert(0, str(_here.parent / "python_bw_src"))
from dotenv import load_dotenv                      # noqa: E402
load_dotenv(_here.parent / "python_bw_src" / ".env")
from db import engine                               # noqa: E402
from sqlalchemy import text                         # noqa: E402

IGNORE = {"ignore", "delete"}
LAT, LON = 51.5, 10.0                    # BW_GEO_LAT_DEG / BW_GEO_LON_DEG in config.h
BERLIN = ZoneInfo("Europe/Berlin")       # the DS3231 runs Berlin local time

# ── table geometry.  Must match presuppress.c exactly. ───────────────────────
# Bands are fixed (not quantiles) so the C side needs no table of edges beyond
# these, and so the meaning of a cell does not drift when refitted.
ELEV_EDGES  = [0.0, 5.0, 10.0, 20.0, 30.0, 40.0, 50.0]     # -> 8 bands
GAP_EDGES   = [15.0, 60.0, 300.0, 1800.0, 7200.0]          # seconds -> 6 bands
BURST_MAX   = 3                                            # 0,1,2,>=3 -> 4 bands
N_ELEV, N_GAP, N_BURST = len(ELEV_EDGES) + 1, len(GAP_EDGES) + 1, BURST_MAX + 1
N_CELLS = N_ELEV * N_GAP * N_BURST


def solar_elevation(dt_local) -> float:
    """NOAA solar elevation in degrees.  Mirrored in C — keep the two in step."""
    dt = dt_local.replace(tzinfo=BERLIN).astimezone(timezone.utc)
    day = dt.timetuple().tm_yday
    hour = dt.hour + dt.minute / 60 + dt.second / 3600
    g = 2 * math.pi / 365 * (day - 1 + (hour - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    ha = math.radians((hour * 60 + eqtime + 4 * LON) / 4 - 180)
    lat = math.radians(LAT)
    cz = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha)
    return 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, cz))))


def band(value, edges) -> int:
    i = 0
    for e in edges:
        if value >= e:
            i += 1
    return i


def cell_index(elev, gap, burst) -> int:
    return int((band(elev, ELEV_EDGES) * N_GAP + band(gap, GAP_EDGES)) * N_BURST
               + min(int(burst), BURST_MAX))


def load_events():
    """One record per PIR event.  No images are read — the rule is clock-only."""
    with engine.connect() as c:
        rows = c.execute(text("""
            select id, captured_at, coalesce(meta->>'label','') lbl
            from bw_frames
            where meta->>'source' = 'pir'
              and coalesce(meta->>'label','') not in ('ignore','delete')
            order by captured_at asc""")).all()
    out, prev, burst = [], None, 0
    for r in rows:
        gap = 1e6 if prev is None else (r.captured_at - prev).total_seconds()
        burst = burst + 1 if gap < 60 else 0
        out.append(dict(id=r.id, date=str(r.captured_at.date()),
                        bird=int(r.lbl == "bird"),
                        elev=solar_elevation(r.captured_at),
                        gap=gap, burst=burst))
        prev = r.captured_at
    return out


def cell_centroids():
    """A representative (elev, gap, burst) for every cell, for distillation."""
    def mids(edges, lo, hi):
        pts = [lo] + list(edges) + [hi]
        return [(pts[i] + pts[i + 1]) / 2 for i in range(len(pts) - 1)]
    ev = mids(ELEV_EDGES, -10.0, 65.0)
    gv = mids(GAP_EDGES, 0.0, 14400.0)
    out = np.zeros((N_CELLS, 3))
    for a, e in enumerate(ev):
        for b, gp in enumerate(gv):
            for c in range(N_BURST):
                out[(a * N_GAP + b) * N_BURST + c] = (e, gp, c)
    return out


def fit_table_empirical(recs, prior_strength=4.0):
    """Per-cell bird rate, Laplace-smoothed toward the global rate.

    Kept for comparison only.  It generalises poorly: hard cell edges plus 68
    birds over 192 cells means a held-out bird often lands in a cell that saw
    none in training, and gets a near-zero score.
    """
    base = np.mean([r["bird"] for r in recs]) if recs else 0.0
    n = np.zeros(N_CELLS); k = np.zeros(N_CELLS)
    for r in recs:
        i = cell_index(r["elev"], r["gap"], r["burst"])
        n[i] += 1; k[i] += r["bird"]
    return (k + prior_strength * base) / (n + prior_strength)


def fit_table_distilled(Xtr, ytr, centroids):
    """DISTIL the model into the table: evaluate the fitted model at each cell
    centroid and cache the answer.  The model does the generalising across
    feature space; the table is only a lookup of its decision surface, so the
    table inherits the model's behaviour instead of re-learning from raw counts.
    """
    m = HistGradientBoostingClassifier(max_depth=3, max_iter=250,
                                       learning_rate=0.06, random_state=0)
    m.fit(Xtr, ytr)
    return m.predict_proba(centroids)[:, 1]


def curve(scores, y, order_fracs=(0.10, 0.20, 0.30, 0.40, 0.50)):
    """Threshold-based, not rank-based.

    A lookup table gives many frames the identical score, and the device
    suppresses by comparing that score to a threshold — so whole cells go
    together.  Ranking with argsort would split tied cells arbitrarily and
    report tie-breaking luck rather than the rule's real behaviour.  For the
    continuous model the two are equivalent, so the comparison stays fair.
    """
    scores = np.asarray(scores, float)
    cuts = np.unique(scores)
    stats = []
    for t in cuts:
        m = scores < t
        stats.append((float(m.mean()), int(y[m].sum())))
    stats.append((1.0, int(y.sum())))

    out = []
    for f in order_fracs:
        ok = [s for s in stats if s[0] <= f + 1e-12]
        out.append(max(ok, key=lambda s: s[0])[1] if ok else 0)
    zero = max((s[0] for s in stats if s[1] == 0), default=0.0)
    return out, 100 * zero


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", action="store_true", help="write presuppress_table.h")
    ap.add_argument("--target", type=float, default=0.35,
                    help="suppression fraction the exported threshold aims at")
    args = ap.parse_args()

    recs = load_events()
    y = np.array([r["bird"] for r in recs])
    g = np.array([r["date"] for r in recs])
    X = np.array([[r["elev"], r["gap"], r["burst"]] for r in recs], float)
    print(f"{len(recs)} PIR events, {y.sum()} birds, {len(set(g))} days")
    print(f"table geometry: {N_ELEV} elev x {N_GAP} gap x {N_BURST} burst = {N_CELLS} cells\n")

    fracs = (0.10, 0.20, 0.30, 0.40, 0.50)
    hdr = "  ".join(f"{int(f*100):>3}%" for f in fracs)
    print(f"{'':>34} {hdr}   zero-loss")

    # reference: the gradient-boosted model the table has to match
    oof = np.zeros(len(X))
    for tr, te in GroupKFold(n_splits=5).split(X, y, groups=g):
        m = HistGradientBoostingClassifier(max_depth=3, max_iter=250,
                                           learning_rate=0.06, random_state=0)
        m.fit(X[tr], y[tr])
        oof[te] = m.predict_proba(X[te])[:, 1]
    lost, zero = curve(oof, y, fracs)
    print(f"{'gradient-boosted (reference)':>34} " +
          "  ".join(f"{v:>4}" for v in lost) + f"   {zero:5.1f}%")

    cent = cell_centroids()
    oof_e = np.zeros(len(X))
    oof_t = np.zeros(len(X))
    for tr, te in GroupKFold(n_splits=5).split(X, y, groups=g):
        tbl_e = fit_table_empirical([recs[i] for i in tr])
        tbl_t = fit_table_distilled(X[tr], y[tr], cent)
        idx = [cell_index(*X[i]) for i in te]
        oof_e[te] = tbl_e[idx]
        oof_t[te] = tbl_t[idx]
    lost_e, zero_e = curve(oof_e, y, fracs)
    print(f"{'table, empirical cell rates':>34} " +
          "  ".join(f"{v:>4}" for v in lost_e) + f"   {zero_e:5.1f}%")
    lost_t, zero_t = curve(oof_t, y, fracs)
    print(f"{'table, distilled from model':>34} " +
          "  ".join(f"{v:>4}" for v in lost_t) + f"   {zero_t:5.1f}%")

    print(f"\nGATE: the table must not be materially worse than the model.")
    verdict = "PASS" if zero_t >= zero * 0.85 else "FAIL"
    print(f"  zero-loss suppression  model {zero:.1f}%  table {zero_t:.1f}%  -> {verdict}")

    if args.export:
        # Ship a table fitted on ALL data, then derive the threshold from that
        # same table's scores over the real events — quantising first so the
        # threshold is in exactly the units the firmware compares against.
        tbl = fit_table_distilled(X, y, cent)
        # Rank-based quantisation, not linear.  The scores are probabilities
        # clustered near the 3.7% base rate, so scaling by the max would crush
        # almost every cell into 0-7 and throw away the resolution the threshold
        # needs.  The device only compares score to threshold, so any monotonic
        # transform is safe — spread the distinct values evenly over 0-255.
        uniq = np.unique(tbl)
        spread = np.linspace(0, 255, len(uniq))
        q = np.round(np.interp(tbl, uniq, spread)).astype(int)
        ev_scores = np.array([q[cell_index(*X[i])] for i in range(len(X))])
        # Whole cells cross the threshold together, so a plain quantile lands
        # short.  Sweep the candidate cut points and take the largest that stays
        # within the target suppression.
        cands = [(float((ev_scores < t).mean()), int(t))
                 for t in np.unique(np.append(q, 256))]
        cands = [c for c in cands if c[0] > 0]
        got, thr_b = min(cands, key=lambda c: abs(c[0] - args.target))
        lost_at_thr = int(y[ev_scores < thr_b].sum())
        # Honest bird loss at this operating point: out-of-fold, not in-sample.
        oof_t_at = float(np.quantile(oof_t, got))
        oof_lost = int(y[oof_t <= oof_t_at].sum())

        print("\nachievable operating points near the target:")
        for f, t in sorted(set(cands)):
            if abs(f - args.target) < 0.25:
                mark = "  <- exported" if t == thr_b else ""
                print(f"    thr {t:>3}  suppress {100*f:5.1f}%  "
                      f"in-sample {int(y[ev_scores < t].sum()):>2}/68{mark}")
        print(f"\nexported threshold {thr_b}: suppresses {100*got:.1f}%, "
              f"in-sample loss {lost_at_thr}/{int(y.sum())}, "
              f"OUT-OF-FOLD loss {oof_lost}/{int(y.sum())} <- the honest number")
        path = _here.parent / "esp_bw_src" / "main" / "presuppress_table.h"
        with open(path, "w") as f:
            f.write(f"""// Generated by src/cloud-check/presuppress_model.py — do not edit by hand.
// Suppression score per (solar elevation x quiet gap x burst position) cell.
// Fitted on {len(recs)} PIR events / {int(y.sum())} labelled birds over {len(set(g))} days.
// Cross-validated zero-loss suppression: {zero_t:.1f}% (reference model {zero:.1f}%).
// Shipped threshold {thr_b} suppresses {100*got:.1f}% of PIR events.
// Out-of-fold bird loss at that point: {oof_lost}/{int(y.sum())} — those are NOT lost,
// they arrive as `batched` rows with a thumbnail for review.
#pragma once
#include <stdint.h>

#define BW_PS_N_ELEV   {N_ELEV}
#define BW_PS_N_GAP    {N_GAP}
#define BW_PS_N_BURST  {N_BURST}
#define BW_PS_BURST_MAX {BURST_MAX}

// Band edges — must match presuppress_model.py exactly.
static const float BW_PS_ELEV_EDGES[{len(ELEV_EDGES)}] = {{{', '.join(f'{e:.1f}f' for e in ELEV_EDGES)}}};
static const float BW_PS_GAP_EDGES[{len(GAP_EDGES)}]  = {{{', '.join(f'{e:.1f}f' for e in GAP_EDGES)}}};

// Default threshold for ~{int(args.target*100)}% suppression; overridden by NVS "ps_thr".
#define BW_PS_DEFAULT_THRESHOLD {thr_b}

// Score 0-255; suppress when score < threshold.
static const uint8_t BW_PS_TABLE[{N_CELLS}] = {{
""")
            for i in range(0, N_CELLS, 12):
                f.write("    " + ", ".join(f"{v:3d}" for v in q[i:i + 12]) + ",\n")
            f.write("};\n")
        print(f"\nwrote {path}  ({N_CELLS} cells, threshold {thr_b})")


if __name__ == "__main__":
    main()
