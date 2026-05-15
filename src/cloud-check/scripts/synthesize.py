"""Synthesise augmented training data:

  * birds_paste/  : take an empty sun/ frame and paste a bird-shaped crop into a
                    plausible location on the railing or floor.
  * lighting/     : take an empty sun/ frame and apply brightness/saturation/
                    contrast shifts to expand the cloud-class lighting variance.

Output goes to cloud-check/synth-data/ with filenames that include a hh:mm
timestamp so the existing dataset loader picks up the hour bucket. The synth-
data is NOT auto-included in evaluate.py — run with --include-synth to mix it
in. This keeps the headline metric on real data only.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_check.dataset import DATASET_ROOT  # noqa: E402

OUT_ROOT = Path(__file__).resolve().parents[1] / "synth-data"


def _list(folder: str) -> list[Path]:
    return sorted((DATASET_ROOT / folder).glob("*.jpg"))


def synth_lighting(n: int, seed: int = 0) -> int:
    out = OUT_ROOT / "lighting"
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    base_files = _list("real-data/sun")
    if not base_files:
        return 0
    written = 0
    for i in range(n):
        src = rng.choice(base_files)
        with Image.open(src) as im:
            im = im.convert("RGB")
            brightness = rng.uniform(0.55, 1.55)
            contrast = rng.uniform(0.7, 1.4)
            saturation = rng.uniform(0.4, 1.4)
            im = ImageEnhance.Brightness(im).enhance(brightness)
            im = ImageEnhance.Contrast(im).enhance(contrast)
            im = ImageEnhance.Color(im).enhance(saturation)
            stem = src.stem.split("_")[0]
            hh = rng.randint(7, 20)
            mm = rng.randint(0, 59)
            ss = rng.randint(0, 59)
            name = f"{stem}_{hh:02d}{mm:02d}{ss:02d}_synthL{i:03d}.jpg"
            im.save(out / name, "JPEG", quality=82)
        written += 1
    return written


def synth_bird_paste(n: int, seed: int = 1) -> int:
    """Paste a small dark blob onto the railing-line area of an empty sun frame.

    We deliberately use a procedural "bird silhouette" instead of cropping real
    birds — this keeps the augmentation self-contained and matches the worst
    case (a small, dark, unfamiliar object) without bringing in 2025-domain
    pixels that could leak through.
    """
    out = OUT_ROOT / "birds_paste"
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    base_files = _list("real-data/sun")
    if not base_files:
        return 0
    written = 0
    for i in range(n):
        src = rng.choice(base_files)
        with Image.open(src) as im:
            im = im.convert("RGB").copy()
            w, h = im.size
            blob_w = rng.randint(80, 160)
            blob_h = rng.randint(60, 120)
            # Plausible bird zones in this scene: along the railing band ~y=520..720
            # at SXGA 1600x1200, or on the floor ~y=900..1100.
            zone = rng.choice(["railing", "floor"])
            if zone == "railing":
                y0 = rng.randint(int(h * 0.40), int(h * 0.58))
            else:
                y0 = rng.randint(int(h * 0.72), int(h * 0.85))
            x0 = rng.randint(int(w * 0.20), int(w * 0.75))
            # Procedural bird-ish ellipse with a darker head bump.
            blob = Image.new("RGBA", (blob_w, blob_h), (0, 0, 0, 0))
            arr = np.zeros((blob_h, blob_w, 4), dtype=np.uint8)
            yy, xx = np.mgrid[0:blob_h, 0:blob_w]
            cx, cy = blob_w * 0.5, blob_h * 0.55
            body = (((xx - cx) / (blob_w * 0.45)) ** 2 +
                    ((yy - cy) / (blob_h * 0.40)) ** 2) <= 1.0
            head = (((xx - blob_w * 0.78) / (blob_w * 0.18)) ** 2 +
                    ((yy - blob_h * 0.30) / (blob_h * 0.20)) ** 2) <= 1.0
            mask = body | head
            color = rng.choice([(35, 30, 30), (55, 40, 30), (20, 18, 16)])
            arr[mask, 0:3] = color
            arr[mask, 3] = 230
            blob = Image.fromarray(arr, "RGBA")
            im.paste(blob, (x0, y0), blob)
            stem = src.stem.split("_")[0]
            hh = rng.randint(7, 20)
            mm = rng.randint(0, 59)
            ss = rng.randint(0, 59)
            name = f"{stem}_{hh:02d}{mm:02d}{ss:02d}_synthB{i:03d}.jpg"
            im.save(out / name, "JPEG", quality=82)
        written += 1
    return written


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-lighting", type=int, default=60)
    ap.add_argument("--n-birds", type=int, default=40)
    args = ap.parse_args()

    a = synth_lighting(args.n_lighting)
    b = synth_bird_paste(args.n_birds)
    print(f"wrote {a} lighting augmentations -> {OUT_ROOT/'lighting'}")
    print(f"wrote {b} bird-paste augmentations -> {OUT_ROOT/'birds_paste'}")


if __name__ == "__main__":
    main()
