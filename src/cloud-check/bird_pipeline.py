#!/usr/bin/env python3
"""
bird_pipeline.py — bird / scene-event detector with a visual audit trail.

Successor to regime_diff.py.  Three changes, each driven by a measured failure
of the previous version:

 1. PER-PIXEL, PER-REGIME LEARNED RESIDUAL STATISTICS.
    regime_diff.py suppressed plant flutter and static edges with hard
    multiplicative masks (exp(-(std/scale)^2) * exp(-(grad/scale)^2)).  Those
    masks blacked out most of the foreground and cost bird #1991 (a pigeon
    perched on the railing edge — exactly where the edge mask bites).  A mask
    cannot distinguish "this pixel is always noisy" from "this pixel is noisy
    right now", so it has to throw away the whole region.
    Instead we learn, per pixel and per regime, the *distribution* of the
    detector's own residual over RTC frames, and threshold on a z-score.
    Fluttering foliage has a naturally wide distribution -> tolerant.
    Smooth tile floor has a narrow one -> stays sensitive.  A bird on the
    railing is still an outlier FOR THAT PIXEL, so it survives.

 2. HORPRASERT BRIGHTNESS / CHROMATICITY DECOMPOSITION (RGB, not YUV).
    A shadow changes illumination, not material: it moves the brightness
    distortion `alpha` and leaves the colour distortion `CD` near zero.
    Validated 2026-06-23: on the dappled-foliage shadow that dominates the
    false positives, the old |luma diff| fired on 190k-334k px; CD fired on 0-1.
    But CD is blind to dark backlit pigeon silhouettes (near-zero chroma), and
    the alpha channel that catches them floods on the same dapple.  Hence:

 3. STRUCTURE-OCCLUSION AS A CLASSIFIER (not a mask).
    Under a shadow the background's high-frequency detail (tile grout, railing)
    SURVIVES, merely scaled by the illumination factor.  Under an opaque bird it
    VANISHES.  So compare the frame's local HF energy against `alpha * model HF`
    — the amount a shadow would predict.  This gates the brightness channel.
    Crucially it is applied ONLY where the background actually has structure to
    occlude; on smooth background (open sky) there is nothing to occlude, and a
    shadow cannot fall there either, so the brightness channel stands alone.

Detector  =  chroma-outlier  OR  (darkening-outlier AND structure-occluded)

Trusted inputs only: `source` (rtc/pir) and the human `label`.  The ESP
`result` (clouds/process) is an on-device estimate and is used NOWHERE.

RTC frames are the background/negative set (the PIR did not fire) and are the
only frames that update a model.  PIR and ignore/delete frames never do.

Usage:
    source .venv/bin/activate
    python bird_pipeline.py --from-frame 500 --render interesting
    python bird_pipeline.py --no-render                 # fast threshold sweep
    python bird_pipeline.py --gallery-only              # rebuild HTML from index.csv
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
from db import BwFrame, Session            # noqa: E402
from align_probe import prep, phase_shift  # noqa: E402  (registration, measured in align_probe)

CACHE = _here / ".jpg_cache"
IGNORE_LABELS = {"ignore", "delete"}

# ── defaults (all overridable on the CLI) ────────────────────────────────────
EMA_ALPHA     = 0.15    # per-pixel EMA rate for model mean / variance
STAT_ALPHA    = 0.10    # slower EMA for the learned residual statistics
WARMUP        = 25      # RTC updates before a model is trusted for scoring.
                        # This gates on VARIANCE convergence, not just the mean:
                        # with stat_alpha=0.10 the learned spread needs ~30 samples.
MAX_REGIMES   = 5
SPAWN_DIST    = 0.18
SIG_FLOOR     = 3.0     # DN; floor on per-channel std (JPEG noise), avoids /0 blow-up
CD_Z          = 6.0     # chroma-outlier threshold, in per-pixel sigmas
AD_Z          = 6.0     # darkening-outlier threshold, in per-pixel sigmas
OCC_THR       = 0.35    # fraction of background HF energy that must vanish
HF_MIN        = 2.0     # DN; below this the background has no structure to occlude
HF_K          = 5       # px; local window for high-frequency energy
ALPHA_BG_K    = 51      # px; window for the local illumination reference
SMOOTH        = 3       # px; box filter on the score map before blobbing
BLOB_MIN_AREA = 12      # px
NOVELTY       = 0.60    # rdist above this = lighting state never seen in RTC training
BIG_BLOB_FRAC = 0.04    # fraction of frame; above this = scene-wide change, not a bird
NIGHT_MEAN    = 60.0    # DN; below this the scene is too dark to score, and
                        # such frames would otherwise spawn their own regimes
BUMP_PX       = 6.0     # registration shift above this (confident) = camera moved.
                        # Measured: within-day steps are ~0.04px and real bumps ~10px,
                        # but dusk->dawn pairs show a ~3px artifact, so sit above it.


# ── scene descriptor: 3 global numbers, no hard-coded regions ────────────────
def regime_descriptor(y: np.ndarray) -> np.ndarray:
    """[mean/255, std/64, bright-quartile/dark-quartile ratio /8].

    mean+std give overall brightness and contrast; the quartile ratio gives
    dynamic range (sunny = bright sky over dark shadow -> high; overcast = flat).
    Measured to settle the bank on ~5 regimes split mainly on that ratio.
    """
    flat = y.ravel()
    p25, p75 = np.percentile(flat, [25, 75])
    hi = float(flat[flat >= p75].mean())
    lo = float(flat[flat <= p25].mean())
    return np.array([float(flat.mean()) / 255.0,
                     float(flat.std()) / 64.0,
                     (hi / max(lo, 1.0)) / 8.0], dtype=np.float32)


def fetch_jpg(filename: str, photo_server: str) -> bytes:
    CACHE.mkdir(exist_ok=True)
    local = CACHE / filename
    if local.exists():
        return local.read_bytes()
    data = urllib.request.urlopen(
        f"{photo_server.rstrip('/')}/static/{filename}", timeout=20).read()
    local.write_bytes(data)
    return data


def load_rgb(filename: str, photo_server: str, size) -> np.ndarray:
    """(H, W, 3) float32 RGB.  Horprasert is defined in RGB, not YUV."""
    im = Image.open(io.BytesIO(fetch_jpg(filename, photo_server))).convert("RGB")
    return np.asarray(im.resize(size, Image.BILINEAR), dtype=np.float32)


def gray(rgb: np.ndarray) -> np.ndarray:
    return rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)


def hf_energy(g: np.ndarray, k: int) -> np.ndarray:
    """Local high-frequency energy: mean |g - blur(g)| in a k-px window."""
    return ndimage.uniform_filter(np.abs(g - ndimage.uniform_filter(g, k)), k)


# ── Horprasert brightness / chromaticity decomposition ───────────────────────
def horprasert(rgb: np.ndarray, mean: np.ndarray, sig: np.ndarray):
    """Return (alpha, CD).

    alpha = argmin_a ||(I - a*E)/sig||^2  — the brightness scaling that best
    explains the pixel, ~1 on background, <1 in shadow, >1 in highlight.
    CD    = the residual that brightness scaling could NOT explain, i.e. a real
    change of material/colour.  A shadow moves alpha and leaves CD ~ 0.
    """
    s2 = np.maximum(sig, SIG_FLOOR) ** 2
    num = np.sum(rgb * mean / s2, axis=2)
    den = np.sum(mean * mean / s2, axis=2)
    alpha = num / np.maximum(den, 1e-6)
    resid = (rgb - alpha[..., None] * mean) / np.maximum(sig, SIG_FLOOR)
    return alpha, np.sqrt(np.sum(resid * resid, axis=2))


def blobs(mask: np.ndarray, min_area: int, want_boxes: bool = True):
    """8-connect a boolean mask, drop specks.  Returns (count, max_area, boxes).

    Areas come from one bincount and boxes from one find_objects pass — a
    per-label `np.where` here is O(labels x pixels) and dominated the threshold
    sweep, which labels the same frame dozens of times.
    """
    lbl, n = ndimage.label(mask, structure=np.ones((3, 3), bool))
    if n == 0:
        return 0, 0, []
    counts = np.bincount(lbl.ravel())
    counts[0] = 0
    keep = np.flatnonzero(counts >= min_area)
    if keep.size == 0:
        return 0, 0, []
    boxes = []
    if want_boxes:
        found = ndimage.find_objects(lbl)
        for i in keep:
            sy, sx = found[i - 1]
            boxes.append((int(sx.start), int(sy.start), int(sx.stop - 1), int(sy.stop - 1)))
    return int(keep.size), int(counts[keep].max()), boxes


# ── regime model bank ────────────────────────────────────────────────────────
class Bank:
    """A small set of per-pixel background models, each keyed by a lighting
    descriptor.  Each model holds, per pixel:

        mean, var   RGB background mean and variance  (for Horprasert)
        cd_mu/_var  the distribution of CD observed on RTC frames  <- learned
        ad_mu/_var  the distribution of alpha-darkening on RTC frames  <- learned
        hf_mu       expected local HF energy (for structure occlusion)

    The cd_/ad_ statistics are what replaces the old hard suppression masks.
    """

    def __init__(self, max_regimes, alpha, stat_alpha, spawn_dist):
        self.models, self.max = [], max_regimes
        self.alpha, self.stat_alpha, self.spawn_dist = alpha, stat_alpha, spawn_dist

    def match(self, desc):
        if not self.models:
            return None, float("inf")
        d = [float(np.linalg.norm(desc - m["desc"])) for m in self.models]
        i = int(np.argmin(d))
        return i, d[i]

    def spawn(self, rgb, desc):
        g = gray(rgb)
        self.models.append(dict(
            mean=rgb.copy(), var=np.full_like(rgb, SIG_FLOOR ** 2),
            cd_mu=np.zeros(g.shape, np.float32), cd_var=np.ones(g.shape, np.float32),
            ad_mu=np.zeros(g.shape, np.float32), ad_var=np.full(g.shape, 9e-4, np.float32),
            hf_mu=hf_energy(g, HF_K), desc=desc.copy(), count=1))
        if len(self.models) > self.max:
            self._merge_nearest()
        return len(self.models) - 1

    def update(self, i, rgb, desc, cd, ad):
        """Fold one RTC frame into model i.  `cd`/`ad` are this frame's residuals
        measured against the model BEFORE this update — i.e. genuine background
        prediction error, which is exactly the distribution we want to learn."""
        m, a, sa = self.models[i], self.alpha, self.stat_alpha
        d = rgb - m["mean"]
        m["mean"] += a * d
        m["var"] = (1 - a) * (m["var"] + a * d * d)
        for key, val in (("cd", cd), ("ad", ad)):
            dv = val - m[key + "_mu"]
            m[key + "_mu"] += sa * dv
            m[key + "_var"] = (1 - sa) * (m[key + "_var"] + sa * dv * dv)
        m["hf_mu"] += a * (hf_energy(gray(rgb), HF_K) - m["hf_mu"])
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
        for k in ("mean", "var", "cd_mu", "cd_var", "ad_mu", "ad_var", "hf_mu"):
            a[k] = (a[k] * na + b[k] * nb) / w
        a["desc"] = (a["desc"] * na + b["desc"] * nb) / w
        a["count"] = w
        self.models.pop(bj)

    ARRAYS = ("mean", "var", "cd_mu", "cd_var", "ad_mu", "ad_var", "hf_mu", "desc")

    def save(self, path):
        blob = {}
        for i, m in enumerate(self.models):
            for k in self.ARRAYS:
                blob[f"{i}/{k}"] = m[k]
            blob[f"{i}/count"] = np.array(m["count"])
        np.savez_compressed(path, n=np.array(len(self.models)), **blob)

    def load(self, path):
        z = np.load(path)
        self.models = []
        for i in range(int(z["n"])):
            m = {k: z[f"{i}/{k}"] for k in self.ARRAYS}
            m["count"] = int(z[f"{i}/count"])
            self.models.append(m)
        return len(self.models)

    @staticmethod
    def snapshot(m):
        """Copy the fields needed for scoring, so a frame is always scored against
        the model state PRIOR to its own contribution."""
        return {k: (m[k].copy() if isinstance(m[k], np.ndarray) else m[k])
                for k in ("mean", "var", "cd_mu", "cd_var", "ad_mu", "ad_var",
                          "hf_mu", "desc", "count")}


CD_SD_FLOOR = 0.5     # CD is already in sigma units; floor its learned spread
AD_SD_FLOOR = 0.03    # local darkening contrast; floor its learned spread

# Decision codes.  Everything except BACKGROUND is reported (recall-first).
REPORTED = ("BUMP", "WARMUP", "SCENE_EVENT", "BIRD_CANDIDATE")


def analyse(rgb, model, rdist, args, ref=None, bumped=False):
    """Run every stage against the matched model.  Returns (metrics, stages).

    `stages` holds the intermediate fields so the renderer can show exactly what
    each phase produced — the whole point is that a decision is auditable.
    """
    g = gray(rgb)
    sig = np.sqrt(np.maximum(model["var"], 0.0))

    # ── phase 1: registration against the previous real frame (never the model
    # mean, which is an EMA average and therefore blurred).  Within a day the
    # camera is stationary to ~0.04px median, so this is not a per-frame
    # correction — it is a detector for the rare discrete bump (2 in 47 days).
    if ref is not None:
        dy, dx, conf = phase_shift(ref, prep(g))
        shift = float(np.hypot(dy, dx))
    else:
        shift, conf = 0.0, 0.0

    # ── phase 2: Horprasert decomposition -> brightness vs colour change
    alpha, cd = horprasert(rgb, model["mean"], sig)
    # alpha_bg = the illumination level of the SURROUNDINGS.  Cloud cover moves
    # the whole frame's alpha, and sky/floor dim at different rates (the failure
    # a single global affine fit could never describe), so the reference has to
    # be local and smooth rather than global.  A bird is small compared with
    # this window, so it barely pulls its own reference.
    alpha_bg = ndimage.uniform_filter(alpha, args.alpha_bg_k)
    # darkening RELATIVE to the local illumination; highlights are not birds
    ad = np.maximum(0.0, alpha_bg - alpha)

    # ── phase 3: normalise against what this pixel NORMALLY does in this regime
    z_cd = (cd - model["cd_mu"]) / np.maximum(np.sqrt(model["cd_var"]), CD_SD_FLOOR)
    z_ad = (ad - model["ad_mu"]) / np.maximum(np.sqrt(model["ad_var"]), AD_SD_FLOOR)

    # ── phase 4: structure occlusion.  A shadow scales the background's HF
    # detail by alpha; an opaque object erases it.  Compare against what a
    # shadow would predict, so a shadow scores ~0 and a bird ~1.
    hf_f = hf_energy(g, HF_K)
    occ = 1.0 - hf_f / (np.maximum(alpha_bg, 0.05) * model["hf_mu"] + 1e-3)
    occ = np.clip(occ, 0.0, 1.0)
    has_structure = model["hf_mu"] > args.hf_min
    # Where the background is smooth there is nothing to occlude — and a shadow
    # cannot fall on open sky either — so the brightness channel stands alone.
    occ_ok = (~has_structure) | (occ > args.occ_thr)

    # ── phase 5: combine.  Normalised so 1.0 == threshold on either channel.
    s_chroma = z_cd / args.cd_z
    s_bright = np.where(occ_ok, z_ad, 0.0) / args.ad_z
    score = np.maximum(s_chroma, s_bright)
    if args.smooth > 1:
        score = ndimage.uniform_filter(score, args.smooth)

    # ── phase 6: blobs
    n_blob, max_blob, boxes = blobs(score > 1.0, args.blob_min_area)
    big = max_blob / float(g.size)
    # which channel is responsible, for the audit trail
    fired = score > 1.0
    ch = "-"
    if fired.any():
        ch = "chroma" if (s_chroma > 1.0)[fired].mean() > 0.5 else "bright"

    # ── phase 7: decide
    if bumped and shift > args.bump_px:
        decision, why = "BUMP", f"camera moved {shift:.1f}px (conf {conf:.0f}) — model invalid"
    elif rdist > args.novelty:
        decision, why = "SCENE_EVENT", f"lighting never seen in RTC training (rdist {rdist:.2f})"
    elif big > args.big_blob_frac:
        decision, why = "SCENE_EVENT", f"change covers {big*100:.1f}% of frame — not bird-sized"
    elif n_blob >= 1:
        decision, why = "BIRD_CANDIDATE", f"{n_blob} compact blob(s), largest {max_blob}px, via {ch}"
    else:
        decision, why = "BACKGROUND", "no pixel exceeds its own learned normal"

    return (dict(n_blob=n_blob, max_blob=max_blob, big_frac=round(big, 5),
                 decision=decision, why=why, channel=ch,
                 shift=round(shift, 2), shift_conf=round(conf, 1),
                 z_cd_max=round(float(z_cd.max()), 1), z_ad_max=round(float(z_ad.max()), 1),
                 occ_max=round(float(occ.max()), 2)),
            dict(alpha=alpha, cd=cd, z_cd=z_cd, z_ad=z_ad, occ=occ, score=score,
                 boxes=boxes, has_structure=has_structure, occ_ok=occ_ok))


def sweep_grid(z_cd, z_ad, occ_ok, args, cd_grid, ad_grid, area_grid):
    """Blob-count each (cd_z, ad_z, min_area) combination for this frame.

    Cheap because the expensive parts — JPEG decode, the model, the Horprasert
    decomposition — are already done; only the thresholding and connected-
    component labelling repeat.  Returns {(cd,ad,area): (n_blob, max_blob)}.
    """
    out = {}
    for cd in cd_grid:
        s_ch = z_cd / cd
        for ad in ad_grid:
            score = np.maximum(s_ch, np.where(occ_ok, z_ad, 0.0) / ad)
            if args.smooth > 1:
                score = ndimage.uniform_filter(score, args.smooth)
            fired = score > 1.0
            for area in area_grid:
                n, mx, _ = blobs(fired, area, want_boxes=False)
                out[(cd, ad, area)] = (n, mx)
    return out


def _panel(ax, data, title, cmap="inferno", vmin=0, vmax=1, boxes=None):
    im = ax.imshow(data, cmap=cmap, vmin=vmin, vmax=vmax)
    ax.set_title(title, fontsize=8.5)
    for x0, y0, x1, y1 in (boxes or []):
        ax.add_patch(plt.Rectangle((x0, y0), x1 - x0, y1 - y0,
                                   edgecolor="lime", facecolor="none", lw=1.5))
    ax.set_xticks([]); ax.set_yticks([])
    return im


def render(meta, rgb, model, ri, rdist, met, st, out_path):
    """Nine panels: every phase, in order, ending in the decision."""
    fig, ax = plt.subplots(3, 3, figsize=(15, 11))
    badge = "  *** BIRD ***" if meta.get("label") == "bird" else (
        f"  [{meta['label']}]" if meta.get("label") else "")

    if model is None:
        for row in ax:
            for a_ in row:
                a_.axis("off")
        ax[0, 0].imshow(rgb.astype(np.uint8)); ax[0, 0].set_title("frame", fontsize=9)
        fig.suptitle(f"#{meta['id']} [{(meta['src'] or '?').upper()}]  model warmup — "
                     f"reported unconditionally{badge}", fontsize=12)
        fig.tight_layout(); fig.savefig(out_path, dpi=72, bbox_inches="tight"); plt.close(fig)
        return

    _panel(ax[0, 0], rgb.astype(np.uint8),
           f"1. frame  #{meta['id']} [{(meta['src'] or '?').upper()}]  {meta['t']}{badge}",
           cmap=None, vmin=None, vmax=None)
    _panel(ax[0, 1], model["mean"].astype(np.uint8),
           f"2. regime {ri} model mean (n={model['count']}, rdist={rdist:.2f})",
           cmap=None, vmin=None, vmax=None)
    _panel(ax[0, 2], st["alpha"], "3. alpha — brightness distortion\n<1 shadow, 1 background, >1 highlight",
           cmap="coolwarm_r", vmin=0.4, vmax=1.6)

    _panel(ax[1, 0], st["cd"], "4. CD — colour distortion (raw)\nshadow leaves this near zero",
           vmax=12)
    _panel(ax[1, 1], model["cd_mu"], "5. LEARNED normal CD per pixel\nbright = this pixel is always restless",
           vmax=12)
    _panel(ax[1, 2], st["z_cd"], f"6. z(CD) — chroma outlier\nthreshold {CD_Z:.0f} sigma", vmax=12)

    _panel(ax[2, 0], st["z_ad"], f"7. z(local darkening) — brightness outlier\ndarker than its SURROUNDINGS, threshold {AD_Z:.0f} sigma", vmax=12)
    a = _panel(ax[2, 1], st["occ"], "8. structure occlusion\n1 = background detail GONE (opaque object)")
    ax[2, 1].contour(st["has_structure"], levels=[0.5], colors="cyan", linewidths=0.5)
    im = _panel(ax[2, 2], st["score"], f"9. combined score (1.0 = fire)\n{met['decision']}",
                vmax=3, boxes=st["boxes"])
    fig.colorbar(im, ax=ax[2, 2], fraction=0.046, pad=0.04)

    colour = {"BIRD_CANDIDATE": "#2a2", "SCENE_EVENT": "#c80", "BUMP": "#c22",
              "BACKGROUND": "#666", "WARMUP": "#666"}[met["decision"]]
    fig.suptitle(f"#{meta['id']} [{(meta['src'] or '?').upper()}]  regime {ri}  →  "
                 f"{met['decision']}  —  {met['why']}{badge}",
                 fontsize=12, color=colour)
    fig.tight_layout(); fig.savefig(out_path, dpi=72, bbox_inches="tight"); plt.close(fig)


GALLERY = r"""<!doctype html><meta charset=utf-8><title>bird pipeline — audit</title>
<style>
 :root{--bg:#111;--fg:#ddd;--dim:#888;--line:#333}
 html,body{background:var(--bg);color:var(--fg);font:13px system-ui,sans-serif;margin:0}
 #wrap{display:flex;min-height:100vh}
 #side{width:310px;flex:none;border-right:1px solid var(--line);padding:12px;box-sizing:border-box}
 #main{flex:1;padding:12px;min-width:0}
 h2{font-size:13px;margin:16px 0 6px;color:#9cf;letter-spacing:.04em;text-transform:uppercase}
 h2:first-child{margin-top:0}
 ol.ph{padding-left:18px;margin:0;color:#bbb} ol.ph li{margin:5px 0;line-height:1.35}
 ol.ph b{color:#eee}
 table.sum{border-collapse:collapse;width:100%} table.sum td{padding:2px 4px;border-bottom:1px solid #222}
 table.sum td:last-child{text-align:right;font-family:ui-monospace,monospace;color:#9c9}
 .bar{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
 button{background:#222;color:var(--fg);border:1px solid #444;padding:5px 10px;border-radius:5px;cursor:pointer;font:inherit}
 button.on{background:#2a6;color:#000;border-color:#2a6}
 button.warn{border-color:#a44;color:#f99} button.warn.on{background:#a33;color:#fff}
 #pos{margin-left:auto;font-family:ui-monospace,monospace;color:#9c9}
 img{width:100%;display:block;cursor:pointer;user-select:none;border-radius:4px}
 #cap{margin-top:8px;font-family:ui-monospace,monospace;color:#bbb;line-height:1.6}
 .pill{display:inline-block;padding:2px 8px;border-radius:10px;font-weight:700;color:#000}
 .hint{color:#666} .k{color:var(--dim)}
</style>
<div id=wrap>
<div id=side>
 <h2>What you are looking at</h2>
 <ol class=ph>
  <li><b>frame</b> — the captured image.</li>
  <li><b>model mean</b> — the learned background for this <i>lighting regime</i>. Built from RTC frames only.</li>
  <li><b>alpha</b> — brightness distortion. A shadow makes this &lt;1 <i>without</i> changing colour.</li>
  <li><b>CD</b> — colour distortion. A shadow leaves it ~0; a real object does not.</li>
  <li><b>learned normal CD</b> — how restless each pixel normally is. Bright = fluttering foliage. This replaces the old hard masks.</li>
  <li><b>z(CD)</b> — panel 4 measured in units of panel 5. Chroma channel.</li>
  <li><b>z(darkening)</b> — same idea on the brightness channel, for dark silhouettes that have no colour.</li>
  <li><b>occlusion</b> — did the background's fine detail survive (shadow) or vanish (opaque bird)? Cyan outline = region with structure to test.</li>
  <li><b>score</b> — chroma OR (brightness AND occluded). Fires at 1.0; boxes are the blobs.</li>
 </ol>
 <h2>Decisions</h2>
 <ol class=ph style="list-style:none;padding-left:0">
  <li><span class=pill style="background:#2a2">BIRD_CANDIDATE</span> small compact blobs → report</li>
  <li><span class=pill style="background:#c80">SCENE_EVENT</span> whole-frame change or unseen light → report</li>
  <li><span class=pill style="background:#c22">BUMP</span> camera moved → model invalid</li>
  <li><span class=pill style="background:#666;color:#fff">BACKGROUND</span> nothing beyond normal → suppress</li>
 </ol>
 <p class=hint>Only BACKGROUND is suppressed. Everything else is uploaded — recall first.</p>
 <h2>Run summary</h2>
 __SUMMARY__
</div>
<div id=main>
 <div class=bar>
  <button id=prev>&#9664;</button><button id=next>&#9654;</button>
  <span class=hint>click image / arrow keys</span>
  <button data-f=all class=on>all</button>
  <button data-f=src:rtc>RTC</button>
  <button data-f=src:pir>PIR</button>
  <button data-f=bird style="border-color:#4a4;color:#cfc">&#128038; birds</button>
  <button data-f=miss class=warn>&#9888; MISSED birds</button>
  <button data-f=fp class=warn>RTC false positives</button>
  <button data-f=dec:SCENE_EVENT>events</button>
  <button data-f=dec:BUMP>bumps</button>
  <span id=pos></span>
 </div>
 <img id=img><div id=cap></div>
</div></div>
<script>
const ALL=__DATA__; let view=ALL.slice(), i=0;
const img=document.getElementById('img'),cap=document.getElementById('cap'),pos=document.getElementById('pos');
const COL={BIRD_CANDIDATE:'#2a2',SCENE_EVENT:'#c80',BUMP:'#c22',BACKGROUND:'#666',WARMUP:'#666',NIGHT:'#446'};
function show(){
 if(!view.length){img.removeAttribute('src');cap.innerHTML='<em>(no frames match)</em>';pos.textContent='';return;}
 i=(i%view.length+view.length)%view.length; const it=view[i];
 img.src=it.file;
 cap.innerHTML=`<span class=pill style="background:${COL[it.dec]||'#666'}">${it.dec}</span> &nbsp;${it.why}<br>`+
  `<span class=k>#</span>${it.id} <span class=k>${it.src.toUpperCase()}</span> ${it.t} `+
  (it.label?`<b style="color:#4f4">${it.label}</b>`:'')+`<br>`+
  `<span class=k>regime</span> ${it.regime} <span class=k>rdist</span> ${it.rdist} `+
  `<span class=k>blobs</span> ${it.n_blob} <span class=k>max</span> ${it.max_blob}px `+
  `<span class=k>via</span> ${it.channel} <span class=k>| z(CD)</span> ${it.z_cd_max} `+
  `<span class=k>z(dark)</span> ${it.z_ad_max} <span class=k>occ</span> ${it.occ_max} `+
  `<span class=k>shift</span> ${it.shift}px`;
 pos.textContent=`${i+1} / ${view.length}`;
}
const step=d=>{i+=d;show()};
prev.onclick=()=>step(-1); next.onclick=()=>step(1);
img.onclick=e=>step(e.offsetX<img.clientWidth/2?-1:1);
addEventListener('keydown',e=>{if(e.key==='ArrowRight'||e.key===' ')step(1);else if(e.key==='ArrowLeft')step(-1)});
document.querySelectorAll('button[data-f]').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('button[data-f]').forEach(x=>x.classList.remove('on')); b.classList.add('on');
 const f=b.dataset.f;
 if(f==='all') view=ALL.slice();
 else if(f==='bird') view=ALL.filter(x=>x.label==='bird');
 else if(f==='miss') view=ALL.filter(x=>x.label==='bird'&&x.dec==='BACKGROUND');
 else if(f==='fp')   view=ALL.filter(x=>x.src==='rtc'&&x.dec!=='BACKGROUND'&&x.dec!=='WARMUP');
 else{const[k,v]=f.split(':'); view=ALL.filter(x=>(k==='src'?x.src:x.dec)===v);}
 i=0;show();
});
show();
</script>"""


def write_gallery(out: Path, rows, summary_html: str):
    items = [dict(file=r["file"], id=r["id"], src=r["src"] or "?", t=r["t"],
                  label=r.get("label") or "", dec=r["decision"], why=r["why"],
                  regime=r["regime"], rdist=r["rdist"], n_blob=r["n_blob"],
                  max_blob=r["max_blob"], channel=r.get("channel", "-"),
                  z_cd_max=r.get("z_cd_max", ""), z_ad_max=r.get("z_ad_max", ""),
                  occ_max=r.get("occ_max", ""), shift=r.get("shift", ""))
             for r in rows if r.get("file")]
    (out / "gallery.html").write_text(
        GALLERY.replace("__DATA__", json.dumps(items)).replace("__SUMMARY__", summary_html))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-frame", type=int, default=500)
    ap.add_argument("--to-frame", type=int, default=None)
    ap.add_argument("--photo-server", default=os.getenv("PHOTO_SERVER", "http://192.168.1.110:8000"))
    ap.add_argument("--out", default=str(_here / "bird_pipeline_out"))
    ap.add_argument("--width", type=int, default=800)
    ap.add_argument("--render", choices=["all", "interesting", "none"], default="interesting",
                    help="'interesting' = birds, every reported frame, and every Nth RTC")
    ap.add_argument("--rtc-sample", type=int, default=12, help="render every Nth RTC background frame")
    ap.add_argument("--no-render", action="store_true", help="alias for --render none (fast sweep)")
    ap.add_argument("--gallery-only", action="store_true")
    ap.add_argument("--save-seed", help="write the converged model bank here (burn-in pass)")
    ap.add_argument("--load-seed", help="start from a converged bank (production pass)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--alpha", type=float, default=EMA_ALPHA)
    ap.add_argument("--stat-alpha", type=float, default=STAT_ALPHA)
    ap.add_argument("--warmup", type=int, default=WARMUP)
    ap.add_argument("--max-regimes", type=int, default=MAX_REGIMES)
    ap.add_argument("--spawn-dist", type=float, default=SPAWN_DIST)
    ap.add_argument("--cd-z", type=float, default=CD_Z)
    ap.add_argument("--ad-z", type=float, default=AD_Z)
    ap.add_argument("--occ-thr", type=float, default=OCC_THR)
    ap.add_argument("--hf-min", type=float, default=HF_MIN)
    ap.add_argument("--alpha-bg-k", type=int, default=ALPHA_BG_K,
                    help="window (px) for the local illumination reference")
    ap.add_argument("--smooth", type=int, default=SMOOTH)
    ap.add_argument("--blob-min-area", type=int, default=BLOB_MIN_AREA)
    ap.add_argument("--novelty", type=float, default=NOVELTY)
    ap.add_argument("--big-blob-frac", type=float, default=BIG_BLOB_FRAC)
    ap.add_argument("--bump-px", type=float, default=BUMP_PX)
    ap.add_argument("--night-mean", type=float, default=NIGHT_MEAN)
    ap.add_argument("--sweep", action="store_true",
                    help="calibrate cd_z/ad_z/min_area against the RTC negative set")
    ap.add_argument("--reset-on-bump", action="store_true", default=True)
    args = ap.parse_args()
    if args.no_render:
        args.render = "none"

    out = Path(args.out); out.mkdir(exist_ok=True)
    cols = ["file", "id", "src", "t", "label", "regime", "rdist", "decision", "why",
            "channel", "n_blob", "max_blob", "big_frac", "z_cd_max", "z_ad_max",
            "occ_max", "shift", "shift_conf"]

    if args.gallery_only:
        rows = list(csv.DictReader(open(out / "index.csv")))
        write_gallery(out, rows, summarise(rows)[1])
        print(f"Rebuilt gallery.html from {len(rows)} rows")
        return

    size = (args.width, int(args.width * 3 / 4))
    q = (Session().query(BwFrame).filter(BwFrame.id >= args.from_frame)
         .filter(BwFrame.filename.isnot(None)))
    if args.to_frame:
        q = q.filter(BwFrame.id <= args.to_frame)
    frames = q.order_by(BwFrame.captured_at.asc()).all()
    print(f"{len(frames)} frames, size={size}, render={args.render}\n"
          f"  cd_z={args.cd_z} ad_z={args.ad_z} occ={args.occ_thr} hf_min={args.hf_min} "
          f"min_area={args.blob_min_area} novelty={args.novelty} bump={args.bump_px}px")

    bank = Bank(args.max_regimes, args.alpha, args.stat_alpha, args.spawn_dist)
    if args.load_seed:
        print(f"  loaded {bank.load(args.load_seed)} regimes from {args.load_seed}")
    rows, n, n_rendered, sweep_rows = [], 0, 0, []
    ref_stable, bump_pending = None, 0   # registration reference + bump confirmation
    for f in frames:
        meta_db = f.meta or {}
        src, label = meta_db.get("source"), (meta_db.get("label") or "")
        is_ignore = label in IGNORE_LABELS
        meta = dict(id=f.id, src=src, label=label,
                    t=f.captured_at.strftime("%Y-%m-%d %H:%M:%S"))
        try:
            rgb = load_rgb(f.filename, args.photo_server, size)
        except Exception as exc:
            print(f"  MISS #{f.id} {f.filename}: {exc}")
            continue

        g0 = gray(rgb)
        if float(g0.mean()) < args.night_mean:
            # too dark to score, and letting it into the bank costs a regime slot
            rows.append(dict(file="", id=f.id, src=src, t=meta["t"], label=label,
                             regime=-1, rdist=0.0, decision="NIGHT",
                             why=f"scene mean {g0.mean():.0f} DN — too dark to score",
                             channel="-", n_blob=0, max_blob=0, big_frac=0.0,
                             z_cd_max=0.0, z_ad_max=0.0, occ_max=0.0,
                             shift=0.0, shift_conf=0.0))
            n += 1
            continue

        desc = regime_descriptor(g0)
        ri, rdist = bank.match(desc)
        snap = Bank.snapshot(bank.models[ri]) if ri is not None else None
        ready = snap is not None and snap["count"] >= args.warmup

        if ready:
            met, st = analyse(rgb, snap, rdist, args, ref=ref_stable,
                              bumped=(bump_pending >= 1 and src == "rtc"))
        else:
            met, st = dict(n_blob=0, max_blob=0, big_frac=0.0, decision="WARMUP",
                           why="model not yet converged — reported unconditionally",
                           channel="-", shift=0.0, shift_conf=0.0,
                           z_cd_max=0.0, z_ad_max=0.0, occ_max=0.0), None

        want = (args.render == "all" or
                (args.render == "interesting" and
                 (label == "bird" or met["decision"] in REPORTED or n % args.rtc_sample == 0)))
        fname = ""
        if args.render != "none" and want:
            fname = f"{n:05d}_{(src or 'x').upper()}_{f.id}{'_BIRD' if label == 'bird' else ''}.png"
            render(meta, rgb, snap if ready else None, ri if ri is not None else -1,
                   rdist, met, st, out / fname)
            n_rendered += 1

        met.update(file=fname, id=f.id, src=src, t=meta["t"], label=label,
                   regime=ri if ri is not None else -1, rdist=round(rdist, 3))
        rows.append(met)

        if args.sweep and ready and st is not None and not is_ignore:
            res = sweep_grid(st["z_cd"], st["z_ad"], st["occ_ok"], args,
                             CD_GRID, AD_GRID, AREA_GRID)
            sweep_rows.append((src, label == "bird", met["decision"], res))

        # ── registration bookkeeping.  Only RTC frames move the reference: a PIR
        # frame may legitimately contain a bird, and a bird is not a camera move.
        if src == "rtc" and not is_ignore:
            if met["shift"] > args.bump_px and met["shift_conf"] > 20 and ref_stable is not None:
                bump_pending += 1          # hold the old reference; wait for confirmation
            else:
                bump_pending = 0
                ref_stable = prep(g0)

        # ── model maintenance: RTC frames only, never PIR, never ignore/delete.
        if src == "rtc" and not is_ignore:
            if met["decision"] == "BUMP" and bump_pending >= 2 and args.reset_on_bump:
                print(f"  #{f.id} BUMP {met['shift']}px confirmed — rebuilding the bank")
                bank.models.clear()
                ri = bank.spawn(rgb, desc)
                ref_stable, bump_pending = prep(g0), 0
            elif ri is None or rdist > args.spawn_dist:
                ri = bank.spawn(rgb, desc)
            else:
                # feed the model the residuals it just produced, so the learned
                # "normal" tracks this pixel's real behaviour in this regime
                cd_obs = np.zeros(rgb.shape[:2], np.float32)
                ad_obs = np.zeros(rgb.shape[:2], np.float32)
                if ready and st is not None:
                    sig = np.sqrt(np.maximum(snap["var"], 0.0))
                    a_, cd_obs = horprasert(rgb, snap["mean"], sig)
                    ad_obs = np.maximum(0.0, ndimage.uniform_filter(a_, args.alpha_bg_k) - a_)
                bank.update(ri, rgb, desc, cd_obs, ad_obs)

        n += 1
        if n % 100 == 0:
            print(f"  {n}/{len(frames)}  #{f.id} K={len(bank.models)} "
                  f"regime={ri} {met['decision']}")
        if args.limit and n >= args.limit:
            break

    if args.save_seed:
        bank.save(args.save_seed)
        print(f"  saved {len(bank.models)} regimes to {args.save_seed}")

    with open(out / "index.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols); w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})

    if args.sweep and sweep_rows:
        report_sweep(sweep_rows, args)

    text, html_sum = summarise(rows)
    write_gallery(out, rows, html_sum)
    print(f"\nWrote {len(rows)} rows ({n_rendered} panels) + index.csv + gallery.html to {out}")
    print(f"final regimes: {len(bank.models)} "
          f"{[[round(float(x), 2) for x in m['desc']] for m in bank.models]}")
    print(text)


CD_GRID   = (6.0, 10.0, 14.0, 20.0, 28.0)
AD_GRID   = (6.0, 10.0, 14.0, 20.0, 28.0)
AREA_GRID = (12, 40, 120)


def report_sweep(sweep_rows, args):
    """Print bird recall against RTC false-positive rate for every grid point.

    The operating point is the one that keeps recall at 100% (a missed bird is
    worse than an extra upload) while suppressing the most RTC background.
    """
    print("\n=== threshold calibration (RTC negatives vs labelled birds) ===")
    print(f"{'cd_z':>5} {'ad_z':>5} {'area':>5} | {'bird recall':>13} | {'RTC FP':>13} | {'PIR kept':>9}")
    best = []
    for cd in CD_GRID:
        for ad in AD_GRID:
            for area in AREA_GRID:
                birds = [r for r in sweep_rows if r[1]]
                rtc = [r for r in sweep_rows if r[0] == "rtc"]
                pir = [r for r in sweep_rows if r[0] == "pir"]
                hit = sum(1 for r in birds if r[3][(cd, ad, area)][0] >= 1)
                fp = sum(1 for r in rtc if r[3][(cd, ad, area)][0] >= 1)
                kept = sum(1 for r in pir if r[3][(cd, ad, area)][0] >= 1)
                rec = hit / max(len(birds), 1)
                fpr = fp / max(len(rtc), 1)
                best.append((rec, -fpr, cd, ad, area, hit, len(birds), fp, len(rtc),
                             kept, len(pir)))
                print(f"{cd:>5.0f} {ad:>5.0f} {area:>5} | {hit:>4}/{len(birds):<4} {rec*100:>5.1f}% "
                      f"| {fp:>4}/{len(rtc):<4} {fpr*100:>5.1f}% | {kept/max(len(pir),1)*100:>8.1f}%")
    full = [b for b in best if b[0] >= 1.0]
    pool = full if full else sorted(best, reverse=True)[:1]
    pick = max(pool, key=lambda b: b[1])
    print(f"\nbest operating point at {'100%' if full else 'max achievable'} recall: "
          f"cd_z={pick[2]:.0f} ad_z={pick[3]:.0f} min_area={pick[4]} -> "
          f"recall {pick[5]}/{pick[6]}, RTC FP {pick[7]}/{pick[8]} ({100*pick[7]/max(pick[8],1):.1f}%), "
          f"PIR kept {100*pick[9]/max(pick[10],1):.1f}%")


def summarise(rows):
    """Return (plaintext, html) — recall on birds and false-positive rate on RTC."""
    def num(r, k):
        try:
            return float(r[k])
        except (TypeError, ValueError):
            return 0.0
    scored = [r for r in rows if r["decision"] not in ("WARMUP", "NIGHT")]
    rtc = [r for r in scored if r["src"] == "rtc"]
    pir = [r for r in scored if r["src"] == "pir"]
    birds = [r for r in rows if (r.get("label") or "") == "bird"]
    b_scored = [r for r in birds if r["decision"] not in ("WARMUP", "NIGHT")]
    b_hit = [r for r in b_scored if r["decision"] != "BACKGROUND"]
    rtc_fp = [r for r in rtc if r["decision"] != "BACKGROUND"]
    pir_sup = [r for r in pir if r["decision"] == "BACKGROUND"]
    misses = [r for r in b_scored if r["decision"] == "BACKGROUND"]

    def pct(a, b):
        return f"{100 * a / max(b, 1):.1f}%"
    t = (f"\nBIRD RECALL      {len(b_hit)}/{len(b_scored)}  ({pct(len(b_hit), len(b_scored))})"
         f"   <- must be 100%\n"
         f"RTC false pos    {len(rtc_fp)}/{len(rtc)}  ({pct(len(rtc_fp), len(rtc))})"
         f"   <- minimise\n"
         f"PIR suppressed   {len(pir_sup)}/{len(pir)}  ({pct(len(pir_sup), len(pir))})"
         f"   <- the workload saved\n")
    if misses:
        t += "MISSED birds: " + ", ".join(f"#{r['id']}" for r in misses) + "\n"
    h = ("<table class=sum>"
         f"<tr><td>bird recall</td><td>{len(b_hit)}/{len(b_scored)} ({pct(len(b_hit), len(b_scored))})</td></tr>"
         f"<tr><td>RTC false positives</td><td>{len(rtc_fp)}/{len(rtc)} ({pct(len(rtc_fp), len(rtc))})</td></tr>"
         f"<tr><td>PIR suppressed</td><td>{len(pir_sup)}/{len(pir)} ({pct(len(pir_sup), len(pir))})</td></tr>"
         f"<tr><td>frames scored</td><td>{len(scored)}</td></tr></table>")
    if misses:
        h += ("<p style='color:#f99'>missed: "
              + ", ".join(f"#{html.escape(str(r['id']))}" for r in misses) + "</p>")
    return t, h


if __name__ == "__main__":
    main()
