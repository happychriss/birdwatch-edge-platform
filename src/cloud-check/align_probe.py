"""
align_probe.py — measure how much the camera physically moves over the dataset.

Question: the sensor is nominally fixed, but is occasionally bumped a few cm.
A per-pixel background model is meaningless across such a move, so before
designing registration we need numbers: how big, how often, sudden or drifting?

Method: phase correlation between consecutive RTC frames (RTC = timer wake, so
the scene is background). Luma is high-pass filtered first so a lighting change
cannot masquerade as a shift, and Hann-windowed to suppress edge wrap.
Reports per-pair shift, a confidence (normalised correlation peak), and the
cumulative sum of shifts, which exposes step changes vs slow drift.

    python align_probe.py --from-frame 1 --width 800
"""
import argparse, csv, io, os, sys, urllib.request
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import uniform_filter

sys.path.insert(0, "/workspace/src/python_bw_src")
from db import Session, BwFrame  # noqa: E402

_here = Path(__file__).parent
CACHE = _here / ".jpg_cache"
IGNORE_LABELS = {"ignore", "delete"}


def fetch_jpg(filename, photo_server):
    CACHE.mkdir(exist_ok=True)
    local = CACHE / filename
    if local.exists():
        return local.read_bytes()
    data = urllib.request.urlopen(
        f"{photo_server.rstrip('/')}/static/{filename}", timeout=20).read()
    local.write_bytes(data)
    return data


def load_luma(filename, photo_server, size):
    im = Image.open(io.BytesIO(fetch_jpg(filename, photo_server))).convert("YCbCr")
    im = im.resize(size, Image.BILINEAR)
    return np.asarray(im, dtype=np.float32)[..., 0]


def prep(y, hp=9):
    """High-pass + Hann window: keep structure, drop illumination and edge wrap."""
    d = y - uniform_filter(y, hp)
    h, w = d.shape
    d = d * np.hanning(h)[:, None] * np.hanning(w)[None, :]
    s = d.std()
    return d / s if s > 1e-6 else d


def _subpix(corr, i, n):
    """Parabolic interpolation around an integer peak index along one axis."""
    a, b, c = corr[(i - 1) % n], corr[i], corr[(i + 1) % n]
    den = a - 2 * b + c
    return 0.0 if abs(den) < 1e-12 else 0.5 * (a - c) / den


def phase_shift(a, b):
    """Displacement of b relative to a, in pixels: (dy, dx, confidence)."""
    FA, FB = np.fft.fft2(a), np.fft.fft2(b)
    R = FA * np.conj(FB)
    mag = np.abs(R)
    R = np.divide(R, mag, out=np.zeros_like(R), where=mag > 1e-12)
    corr = np.real(np.fft.ifft2(R))
    h, w = corr.shape
    iy, ix = np.unravel_index(np.argmax(corr), corr.shape)
    peak = corr[iy, ix]
    dy = iy + _subpix(corr[:, ix], iy, h)
    dx = ix + _subpix(corr[iy, :], ix, w)
    if dy > h / 2: dy -= h
    if dx > w / 2: dx -= w
    # confidence: peak height over the background level of the correlation surface
    conf = float(peak / (corr.std() + 1e-12))
    return float(dy), float(dx), conf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-frame", type=int, default=1)
    ap.add_argument("--to-frame", type=int, default=None)
    ap.add_argument("--width", type=int, default=800)
    ap.add_argument("--min-luma", type=float, default=40.0,
                    help="skip frames darker than this (registration is noise there)")
    ap.add_argument("--photo-server", default=os.getenv("PHOTO_SERVER", "http://192.168.1.110:8000"))
    ap.add_argument("--out", default=str(_here / "align_probe.csv"))
    args = ap.parse_args()

    size = (args.width, int(args.width * 3 / 4))
    s = Session()
    q = (s.query(BwFrame)
         .filter(BwFrame.id >= args.from_frame)
         .filter(BwFrame.filename.isnot(None)))
    if args.to_frame:
        q = q.filter(BwFrame.id <= args.to_frame)
    frames = [f for f in q.order_by(BwFrame.captured_at.asc()).all()
              if (f.meta or {}).get("source") == "rtc"
              and ((f.meta or {}).get("label") or "") not in IGNORE_LABELS]
    print(f"{len(frames)} RTC frames, size={size}")

    rows, prev = [], None
    for n, f in enumerate(frames):
        try:
            y = load_luma(f.filename, args.photo_server, size)
        except Exception as exc:
            print(f"  MISS #{f.id}: {exc}")
            continue
        if y.mean() < args.min_luma:
            prev = None            # break the chain rather than register on noise
            continue
        cur = (f, prep(y))
        if prev is not None:
            dy, dx, conf = phase_shift(prev[1], cur[1])
            gap = (f.captured_at - prev[0].captured_at).total_seconds() / 60.0
            rows.append(dict(id=f.id, prev_id=prev[0].id,
                             t=f.captured_at.isoformat(timespec="seconds"),
                             gap_min=round(gap, 1), dy=round(dy, 3), dx=round(dx, 3),
                             mag=round((dy * dy + dx * dx) ** 0.5, 3),
                             conf=round(conf, 1)))
        prev = cur
        if n % 200 == 0:
            print(f"  ...{n}/{len(frames)}")

    with open(args.out, "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        wr.writeheader(); wr.writerows(rows)

    mag = np.array([r["mag"] for r in rows])
    conf = np.array([r["conf"] for r in rows])
    good = conf > 20
    print(f"\n{len(rows)} pairs written to {args.out}")
    print(f"confident pairs (conf>20): {good.sum()} ({100*good.mean():.0f}%)")
    m = mag[good]
    print("\nconsecutive-RTC shift magnitude, px @ %d wide:" % args.width)
    for p in (50, 75, 90, 95, 99):
        print(f"  p{p:<3} {np.percentile(m, p):7.2f}")
    print(f"  max  {m.max():7.2f}")
    print(f"\npairs shifting >1px: {(m>1).sum()} ({100*(m>1).mean():.1f}%)"
          f"   >3px: {(m>3).sum()} ({100*(m>3).mean():.1f}%)"
          f"   >8px: {(m>8).sum()} ({100*(m>8).mean():.1f}%)")

    print("\n20 largest jumps (confident pairs):")
    idx = np.argsort(-mag * good)[:20]
    for i in sorted(idx, key=lambda j: rows[j]["t"]):
        r = rows[i]
        print(f"  #{r['prev_id']:>5} -> #{r['id']:<5} {r['t']}  gap {r['gap_min']:>6}min "
              f"dy={r['dy']:+7.2f} dx={r['dx']:+7.2f} |{r['mag']:6.2f}| conf {r['conf']}")

    cy = np.cumsum([r["dy"] for r in rows]); cx = np.cumsum([r["dx"] for r in rows])
    print("\ncumulative drift (px) sampled through the dataset:")
    for j in np.linspace(0, len(rows) - 1, 12).astype(int):
        print(f"  {rows[j]['t']}  #{rows[j]['id']:<5} cum dy={cy[j]:+8.1f} dx={cx[j]:+8.1f}")


if __name__ == "__main__":
    main()
