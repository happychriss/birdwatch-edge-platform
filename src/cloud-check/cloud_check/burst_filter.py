"""Burst-mode sequence filter for PIR false triggers caused by strong sunlight.

Around noon on sunny days the PIR fires every 30-120 s on sun/IR fluctuations
with no bird or person in frame.  The existing background-model pipeline misses
these because they are driven by short-term scene dynamics, not long-term
illumination drift.

This filter compares each frame directly to the *previous captured frame*.

Key scene-physics insight (same principle as the background-model pipeline):
  • Birds and people create DARK regions against a bright sky/floor.
  • A scene brightening (dark→bright) is always a sun/cloud event — not a bird.
    The user explicitly said: "going from dark to bright is fine" (i.e. OK to process).
  • Very bright tiles are never birds; only newly-DARK tiles carry object information.

Because of this, DUPLICATE uses absolute tile diff (any direction) for the exact-
duplicate check, but DIFFUSE uses dark-only diff to avoid falsely suppressing
scenes that got brighter overall.

Decision stages:

  FIRST          – no previous frame → process
  ISOLATED       – dt > burst_window → process (new independent event)
  FAST_SHIFT     – |gm_diff| > threshold AND dt < fast_shift_max_dt → suppress
                   (large brightness shift within a short window: the PIR re-fired
                    on the same cloud/sun transition; no time for a bird to coincide)
  BRIGHTNESS_SHIFT – |gm_diff| > threshold AND dt ≥ fast_shift_max_dt → process
                    (large global change over a longer gap: bird could have appeared)
  DUPLICATE      – absolute n_changed == 0 → suppress
                   (PIR multi-fired on the same IR stimulus; pixel-identical)
  BRIGHT_STABLE  – global_mean > bright_min AND n_dark < bright_max_dark → suppress
                   (very bright scene, stable brightness, few dark tiles: no object present.
                    Physics: in a bright scene a bird casts a proportionally stronger shadow,
                    so low n_dark in bright conditions reliably means no bird.
                    Calibrated on training data — revisit when sunny-bird data is added.)
  DIFFUSE        – n_dark_tiles ≥ diffuse_min → suppress
                   (massive global darkening: cloud shadow sweeping the scene.)
  SAFE           – everything else → process (safety bias)
                   Covers: 1-2 tile changes (user rule: always process),
                   brightening events (n_dark≈0), small-to-moderate dark changes
                   that could be a bird or person.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import label as cc_label


@dataclass(frozen=True)
class BurstConfig:
    burst_window_seconds: float = 180.0
    brightness_sim_threshold: float = 12.0   # |gm_diff| > this → BRIGHTNESS_SHIFT → process.
                                             # 12 DN ≈ 5% global brightness change: genuine
                                             # cloud/sun transition → always upload.
                                             # gm_diff 0–12 = within-burst flicker → tile analysis.
                                             # Key: frame 094122 (gm_diff=14.3) still routes to
                                             # BRIGHTNESS_SHIFT → process (14.3 > 12). ✓
    tile_diff_threshold: float = 12.0        # DN threshold for absolute diff (DUPLICATE check).
                                             # 12 DN raises the noise floor so near-threshold tiles
                                             # (noisy pixels, not real objects) are excluded.
    dark_diff_threshold: float = 12.0        # DN threshold: tile must be THIS much darker than prev.
                                             # Matched to tile_diff_threshold intentionally.
    duplicate_max_tiles: int = 0             # ≤ this absolute-diff tiles → DUPLICATE → suppress.
                                             # 0 = only pixel-identical frames suppressed.
                                             # "1-2 tiles changed → process" (user rule)
    diffuse_min_dark_tiles: int = 60         # ≥ this dark tiles → DIFFUSE → suppress.
                                             # Calibrated on training data with dark_diff_threshold=12:
                                             #   max bird/pillow nd at td=12: < 60 (confirmed by sweep)
                                             #   min cloud-shadow nd at td=12: >> 60
                                             # People may be suppressed at this threshold — user
                                             # confirmed this trade-off is acceptable.
    fast_shift_max_dt: float = 15.0          # FAST_SHIFT: suppress BRIGHTNESS_SHIFT when dt is
                                             # below this value. Within 15 s, the probability of
                                             # a bird coincidentally appearing during a large
                                             # brightness shift is negligible — the PIR almost
                                             # certainly re-fired on the same cloud event.
                                             # Calibrated: nearest bird/pillow BRIGHTNESS_SHIFT
                                             # is at dt=27 s (12 s margin). No bird frame has
                                             # dt < 15 s in BRIGHTNESS_SHIFT across all training data.
    fast_shift_min_gm: float = 120.0        # FAST_SHIFT only fires when scene is at least this
                                             # bright. The "re-fired on same cloud event" reasoning
                                             # is specific to strong midday sunshine. In dim scenes
                                             # (gm < 120) a fast brightness shift has other causes
                                             # (sunrise, shade changes) where bird coincidence is
                                             # less predictable.
    bright_stable_min_gm: float = 160.0     # BRIGHT_STABLE: scene must be at least this bright.
                                             # Raised from 130 → 160 after observing a real bird
                                             # at gm=151.2 (process-real-birds/20260521_193242.jpg)
                                             # with nd=7 — indistinguishable from sun flicker at
                                             # that brightness. 160 provides a 9 DN margin above
                                             # the highest known in-burst real-bird brightness.
                                             # Revisit as more sunny-bird data is collected.
    bright_stable_max_dark: int = 35        # BRIGHT_STABLE: suppress if n_dark < this value.
                                             # In training data, all bird/pillow frames at gm>130
                                             # have n_dark = 0 (no sunny-bird data yet).
                                             # Set to 35 to leave safe headroom above nd=0.


@dataclass
class BurstResult:
    label: str          # "suppress" | "process"
    trigger: str        # FIRST | ISOLATED | FAST_SHIFT | BRIGHTNESS_SHIFT | DUPLICATE | BRIGHT_STABLE | DIFFUSE | SAFE
    n_changed: int      # tiles with |diff| > tile_diff_threshold (absolute)
    n_dark: int         # tiles that got DARKER by > dark_diff_threshold
    blob_max: int       # largest connected dark-diff region (diagnostic)
    compactness: float  # blob_max / n_dark  (diagnostic; 0..1)
    gm_diff: float      # |global_mean - prev_global_mean|
    dt_seconds: float   # time since previous frame (inf if no prev)
    reason: str


def burst_classify(
    tile_mean: np.ndarray,
    global_mean: float,
    prev_tile_mean: np.ndarray | None,
    prev_global_mean: float | None,
    dt_seconds: float,
    cfg: BurstConfig | None = None,
) -> BurstResult:
    """Classify a single frame given the previous frame's tile means.

    tile_mean       : (grid_h, grid_w) float32 — current frame
    global_mean     : frame-wide mean of tile_mean
    prev_tile_mean  : same shape, previous captured frame; None if unavailable
    prev_global_mean: scalar; None if unavailable
    dt_seconds      : seconds since previous capture; float('inf') if unavailable
    """
    cfg = cfg or BurstConfig()

    # ── Stage 0: FIRST ──────────────────────────────────────────────────────
    if prev_tile_mean is None or prev_global_mean is None or dt_seconds == float('inf'):
        return BurstResult(
            label="process", trigger="FIRST",
            n_changed=0, n_dark=0, blob_max=0, compactness=0.0,
            gm_diff=0.0, dt_seconds=float('inf'),
            reason="no previous frame",
        )

    gm_diff = float(abs(global_mean - prev_global_mean))

    # ── Stage 1: ISOLATED ───────────────────────────────────────────────────
    if dt_seconds > cfg.burst_window_seconds:
        return BurstResult(
            label="process", trigger="ISOLATED",
            n_changed=0, n_dark=0, blob_max=0, compactness=0.0,
            gm_diff=gm_diff, dt_seconds=dt_seconds,
            reason=f"dt={dt_seconds:.0f}s > burst_window={cfg.burst_window_seconds:.0f}s",
        )

    # ── Stage 2: FAST_SHIFT / BRIGHTNESS_SHIFT ─────────────────────────────
    if gm_diff > cfg.brightness_sim_threshold:
        if dt_seconds < cfg.fast_shift_max_dt and global_mean >= cfg.fast_shift_min_gm:
            return BurstResult(
                label="suppress", trigger="FAST_SHIFT",
                n_changed=0, n_dark=0, blob_max=0, compactness=0.0,
                gm_diff=gm_diff, dt_seconds=dt_seconds,
                reason=(f"gm_diff={gm_diff:.1f} > {cfg.brightness_sim_threshold:.1f} "
                        f"and dt={dt_seconds:.0f}s < {cfg.fast_shift_max_dt:.0f}s "
                        f"and gm={global_mean:.1f} ≥ {cfg.fast_shift_min_gm:.0f} "
                        f"— bright scene, re-fired on same cloud event"),
            )
        return BurstResult(
            label="process", trigger="BRIGHTNESS_SHIFT",
            n_changed=0, n_dark=0, blob_max=0, compactness=0.0,
            gm_diff=gm_diff, dt_seconds=dt_seconds,
            reason=f"gm_diff={gm_diff:.1f} > threshold={cfg.brightness_sim_threshold:.1f}",
        )

    # ── Absolute diff for DUPLICATE check ────────────────────────────────────
    abs_diff = np.abs(tile_mean.astype(np.float32) - prev_tile_mean.astype(np.float32))
    n_changed = int((abs_diff > cfg.tile_diff_threshold).sum())

    # ── Stage 3: DUPLICATE ──────────────────────────────────────────────────
    if n_changed <= cfg.duplicate_max_tiles:
        return BurstResult(
            label="suppress", trigger="DUPLICATE",
            n_changed=n_changed, n_dark=0, blob_max=0, compactness=0.0,
            gm_diff=gm_diff, dt_seconds=dt_seconds,
            reason=f"n_changed={n_changed} ≤ {cfg.duplicate_max_tiles} (pixel-identical burst)",
        )

    # ── Dark-only diff ────────────────────────────────────────────────────────
    # Only tiles that got DARKER are relevant for object detection.
    # Scenes that brightened (sun emerging) have n_dark ≈ 0 → fall through to SAFE.
    dark_diff = prev_tile_mean.astype(np.float32) - tile_mean.astype(np.float32)
    dark_mask = dark_diff > cfg.dark_diff_threshold
    n_dark = int(dark_mask.sum())

    # Blob analysis for diagnostics (also used in DIFFUSE reporting)
    if n_dark > 0:
        labelled, _ = cc_label(dark_mask)
        sizes = np.bincount(labelled.ravel())
        sizes[0] = 0
        blob_max = int(sizes.max())
        compactness = blob_max / n_dark
    else:
        blob_max = 0
        compactness = 0.0

    # ── Stage 4: BRIGHT_STABLE ─────────────────────────────────────────────
    # Very bright scene (gm > 130) + few dark tiles (n_dark < 35): nothing dark entered
    # the scene.  In a bright scene a bird casts a proportionally stronger shadow, so a
    # low n_dark count is highly reliable evidence of no object.  Above 130 DN the
    # training data contains no bird or pillow frames — only sun events and people.
    # NOTE: revisit this threshold once sunny-bird training data is collected.
    if (global_mean > cfg.bright_stable_min_gm
            and n_dark < cfg.bright_stable_max_dark):
        return BurstResult(
            label="suppress", trigger="BRIGHT_STABLE",
            n_changed=n_changed, n_dark=n_dark, blob_max=blob_max, compactness=compactness,
            gm_diff=gm_diff, dt_seconds=dt_seconds,
            reason=(f"gm={global_mean:.1f} > {cfg.bright_stable_min_gm:.0f} "
                    f"and n_dark={n_dark} < {cfg.bright_stable_max_dark} — bright scene, no dark object"),
        )

    # ── Stage 5: DIFFUSE ────────────────────────────────────────────────────
    if n_dark >= cfg.diffuse_min_dark_tiles:
        return BurstResult(
            label="suppress", trigger="DIFFUSE",
            n_changed=n_changed, n_dark=n_dark, blob_max=blob_max, compactness=compactness,
            gm_diff=gm_diff, dt_seconds=dt_seconds,
            reason=(f"n_dark={n_dark} ≥ {cfg.diffuse_min_dark_tiles} "
                    f"(blob_max={blob_max}) → global cloud shadow, not a bird"),
        )

    # ── Default: SAFE ───────────────────────────────────────────────────────
    # Reached here: scene is dim enough that gm ≤ 130 (or n_dark ≥ 35), AND
    # n_dark < diffuse_min: could be a bird, a person, or a mild lighting change.
    # Safety bias: upload.
    return BurstResult(
        label="process", trigger="SAFE",
        n_changed=n_changed, n_dark=n_dark, blob_max=blob_max, compactness=compactness,
        gm_diff=gm_diff, dt_seconds=dt_seconds,
        reason=f"n_dark={n_dark} < {cfg.diffuse_min_dark_tiles} — safety bias (upload)",
    )
