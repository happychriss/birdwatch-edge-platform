# Bird-vs-background diff experiment

Goal: decide if a PIR-triggered frame contains a **new object** (bird / person)
by comparing it to a **self-learned reference of the scene**, robust to moving
shadows, sun↔cloud swings, fluttering plants, and small camera shifts — with
**no hard-coded regions** and improving over time. A bird may be only a few
tiles; pigeons are grey, so colour helps.

## Trusted inputs only

Only these DB fields are used as ground truth / control:

- `source` = `rtc` (15-min reference frame) or `pir` (motion event)
- human `label` = `bird` (positive) ; `ignore` / `delete` (disregard the frame)

The ESP `result` (`clouds` / `process`) is an **on-device estimate and is NOT
used anywhere** — not as a target, not to gate anything.

## How we judge success (no clouds/process labels to score against)

1. **RTC→model diff goes minimal** — RTC frames are background, so a good model
   leaves ~0 blobs. Mean blob count over RTC frames is the loss to minimise.
2. **PIR bird frames keep ≥1 blob** — all labelled birds must still be detected.
   (A person is allowed to be a big blob.)
3. Visual check in the gallery.

## Approach evolution

| Step | Program | Idea | Verdict |
|------|---------|------|---------|
| 0 | `cloud_check/` + `validate.py` (+ ESP `cloud_check.c`) | Production **tile** pipeline (20×15), z-score vs per-tile EMA, runs on ESP32 before WiFi. Coarse, battery-bound. | In production; stays as the cheap on-device pre-filter. |
| 1 | `pixel_delta.py` | First **pixel-level** diff: neighbour frame + single per-pixel EMA model, affine + high-pass illumination normalisation, plant-variance idea (left disabled). Luma only. | Birds pop, but single model can't hold sunny *and* overcast; plant mask never enabled; no colour. Superseded. |
| 2 | `regime_diff.py` | **Regime model bank + colour + masks** (current). See below. | Active. |

## Current model — `regime_diff.py`

- **Regime model bank**: a small set of per-pixel background models, each keyed
  by a 3-number global lighting descriptor (brightness, contrast, bright/dark
  quartile ratio). Frames are matched to the nearest model online; a new model
  spawns when nothing is close; capped at K=5 with nearest-pair merge. This is
  what separates hard-sunny (sharp shadow edges) from flat-overcast — a single
  model would average them and match neither. Self-adjusting, no fixed regions.
- **Time-of-day** is handled *inside* a regime: each model is an EMA over the
  15-min RTC frames, so shadow drift is tracked without explicit clock buckets.
- **Only RTC frames update a model**; PIR and `ignore`/`delete` frames never do.
- **Colour**: each model holds per-pixel Y/U/V. Residual = affine-normalised
  luma diff combined with an AWB-offset-removed chroma diff (grey pigeon vs
  green plant / grey sky).
- **Two masks** suppress non-bird residual, both self-derived:
  - *plant-flutter mask* — per-pixel temporal variance (fluttering foliage).
  - *edge mask* — per-pixel spatial gradient of the model mean (static railing /
    frame lines). A bird appears over a region smooth in the model, so it
    survives; only hard static edges are suppressed.
- **Blobs**: residual is lightly smoothed, masked, thresholded, 8-connected.
  Blob count / largest-blob size is the metric.
- **Scoring is frame-vs-its-matched-model**: RTC→model = background check;
  PIR→model = detection.

### Key parameters (CLI flags)

`--alpha` EMA rate · `--max-regimes` / `--spawn-dist` bank size ·
`--chroma-weight` · `--std-scale` (plant mask) · `--edge-scale` (edge mask) ·
`--smooth` · `--blob-thr` / `--blob-min-area`. `--no-render` = fast stats-only
sweep (skips PNGs). `--gallery-only` = rebuild HTML from an existing index.csv.

### Run it

```bash
cd src/cloud-check && source .venv/bin/activate
python regime_diff.py --from-frame 1877 --photo-server http://192.168.1.110:8000
```

Outputs to `regime_diff_out/`: one PNG per frame (6 panels: frame, regime model
mean, luma residual, chroma residual, suppression mask, masked residual+blobs),
`index.csv` (per-frame metrics), `gallery.html` (filter by RTC / PIR / bird).

## Galleries

| Gallery | Shows |
|---------|-------|
| `regime_diff_out/gallery.html` | **Current experiment.** Per-frame diff vs its regime model, blob counts; filter RTC / PIR / bird. |
| `pixel_delta_out/gallery.html` | Older neighbour-diff prototype (luma, single model). Reference only. |

Served during the session via `python -m http.server 8011` in the gallery dir.

## Results so far (frames 1877+, 606 RTC / ~252 PIR / 11 birds)

| Config | RTC mean blobs | RTC clean (0 blobs) | Birds detected |
|--------|---------------:|--------------------:|---------------:|
| no edge mask | 26.4 | 7% | 11/11 |
| + edge mask (scale 12) | 19.3 | 14% | 10/11 ⚠ |
| + edge mask (scale 16) | 20.5 | 13% | 10/11 ⚠ |
| + edge mask (scale 28) | 22.4 | 11% | 10/11 ⚠ |

**Currently rendered gallery uses edge mask scale 16.** Bank settles on
**5 regimes**, separated mainly on dynamic range (quartile ratio 0.43
flat-overcast → 1.7 hard-sunny) — i.e. the split we wanted.

The edge mask cleans RTC (~26→20 blobs) but **costs bird #1991**, a pigeon
perched directly on the railing edge — exactly where the mask suppresses.
Even a very weak mask (scale 28) loses it, so this is a structural flaw of
masking the residual by the model's gradient: a bird on a railing is on an edge.

## Frame types & aggregate signals (people, door events)

Not every PIR event is a small object on a stable scene. A person opening the
balcony door (e.g. **#1935**) changes the *whole* frame — geometry, light spill,
exposure — so the per-pixel diff cannot (and need not) localise the person; a
person is fine to report. Two aggregate signals separate the cases cleanly:

- **`rdist` (regime novelty)** — distance to the nearest learned regime.
  ~0.05–0.2 for normal frames incl. birds; **1.55 for #1935** (a lighting state
  never seen in RTC training). High `rdist` = novel scene / event.
- **`max_blob` size** — bird clusters are ~20–70 px; #1935's largest blob is
  ~118 k px (~25 % of frame) = scene-wide change.

Intended decision logic (on top of the blob metrics):

| Signal | Meaning | Action |
|--------|---------|--------|
| huge `max_blob` OR high `rdist` | scene event / person / door | report (not bird) |
| a few *small compact* blobs, low `rdist` | bird candidate | report |
| ~0 blobs, low `rdist` | background | suppress |

## Shadow removal via chromaticity — VALIDATED (2026-06-23)

Generic, region-free way to remove shadows: the **Horprasert brightness/chromaticity
decomposition** (classic background-subtraction shadow model). Physical basis — a
shadow changes *illumination*, not *material*; an object changes material.

Per pixel, vs the background mean `E` (per-channel std `sig`), in **RGB**:

- **brightness distortion** `alpha = sum(I*E/sig^2) / sum(E^2/sig^2)` — ~1 background,
  <1 shadow (darker), >1 highlight.
- **color distortion** `CD = ||(I - alpha*E)/sig||` — chromaticity change = real object.

A shadow moves `alpha` and leaves `CD ≈ 0`. (Prototypes: `shadow_proto.py`,
`bird_test.py`, `combined.py` — run from this dir with the cloud-check venv.)

**Benchmark = #2677–#2710** (one sunny day, 31 RTC + 3 PIR; all 3 PIR are
light/shadow false triggers, no bird). The dominant false positive is the dappled
foliage shadow sweeping the smooth tile floor (chroma-neutral). Result:

| Detector | Pixels firing on the dappled shadow |
|---|---|
| old `|luma diff|` | 190k–334k px (massive false positive) |
| **CD (chroma distortion)** | **0–1 px** (shadow invisible) |

**Two constraints this exposed:**

1. CD only catches *coloured/warm* birds — detects #1990, #2404; **misses #2071,
   #2072, #2122, #2581**, which are **dark backlit pigeon silhouettes** (near-zero
   chroma) → CD blind. They are *brightness* events.
2. An `alpha`-darkening channel recovers all 6 birds but **floods on the dapple**
   (40–67 false compact blobs/shadow frame). Shape/compactness alone does **not**
   separate a bird silhouette from a shadow dapple.

**Conclusion — three orthogonal cues, not two:**

| Cue | Catches | Blind to / fooled by |
|---|---|---|
| CD (chroma) | coloured birds, immune to shadow | grey/dark silhouettes; poor bg |
| alpha (brightness) | dark silhouettes on bright bg | dappled shadow |
| **structure-occlusion** | discriminates the above | — |

Structure-occlusion = under a shadow the background's high-freq detail (tile grout,
railing) *survives* (darker); under an opaque bird it *vanishes*. The old high-pass
idea, used as a **classifier** not a mask.

Planned detector: `CD-blob` OR `(alpha-darkening blob AND bg-structure-occluded)`,
replacing the `luma + chroma + harsh plant/edge masks` residual. This also fixes the
"suppression mask too harsh" issue (the plant×edge mask blacks out most of the
foreground — would eat a pigeon). CD also needs a *sharp* background, which couples
this to the cohort/blurry-model problem.

## Step 3 — `bird_pipeline.py` (current)

Replaces the ad-hoc `luma + chroma + harsh plant/edge masks` residual of
`regime_diff.py`. Same regime model bank, but the detector is rebuilt around
three orthogonal cues and, crucially, a **learned per-pixel notion of normal**.

### Phases (each one is a panel in the gallery)

| # | Phase | What it produces |
|---|-------|------------------|
| 1 | frame | the capture |
| 2 | regime match | nearest lighting model + `rdist` |
| 3 | Horprasert `alpha` | brightness distortion — a shadow moves this |
| 4 | Horprasert `CD` | colour distortion — a shadow leaves this ~0 |
| 5 | **learned normal CD** | per-pixel, per-regime EMA of the CD seen on RTC frames |
| 6 | `z(CD)` | phase 4 in units of phase 5 — the chroma channel |
| 7 | `z(local darkening)` | darker than the *surrounding* illumination — the brightness channel |
| 8 | structure occlusion | did the background's fine detail survive (shadow) or vanish (bird)? |
| 9 | score + blobs | `chroma OR (brightness AND occluded)`, fires at 1.0 |

Decision: `BUMP` (camera moved) → `SCENE_EVENT` (unseen light or frame-wide
change) → `BIRD_CANDIDATE` (small compact blobs) → `BACKGROUND` (suppress).
`NIGHT` gates out frames too dark to score. **Only BACKGROUND is suppressed.**

### Why each piece exists (all three were bugs found by measuring)

- **Learned per-pixel statistics replace the hard masks.** The old
  `plant × edge` mask blacked out the foreground and cost bird #1991 (pigeon on
  the railing edge — exactly where the edge mask bites). A mask cannot tell
  "this pixel is always restless" from "this pixel is restless *now*", so it
  discards the whole region. A per-pixel z-score keeps the region and moves the
  bar instead: fluttering foliage earns a wide distribution and becomes
  tolerant; smooth floor keeps a narrow one and stays sensitive.
- **The brightness channel must be a *local* contrast.** Absolute darkening
  `1-alpha` fires on every cloud that crosses the whole scene. Comparing against
  `alpha_bg` (a smooth local illumination reference) makes it immune to both
  global and smooth spatial illumination change — the failure a single global
  affine fit could never describe (sky and floor dim at different rates).
- **Occlusion must divide by the *surrounding* illumination, not the pixel's
  own `alpha`.** At a bird pixel `alpha` is small, which shrank the denominator
  and cancelled the very signal the test exists to find. Using `alpha_bg`, a
  shadow scores ~0 (detail survives, scaled) and a bird ~1 (detail gone).
- **Occlusion is applied only where there is structure to occlude.** On smooth
  background (open sky) nothing can be occluded — and a shadow cannot fall
  there either — so the brightness channel stands alone. This is what saves the
  dark-backlit-pigeon-against-sky case that CD is blind to.

### Two-pass run (mirrors the `backfill_meta.py` convention in CLAUDE.md)

A per-pixel *variance* cannot converge from a cold start: the first frames of
each regime see a near-zero spread, score as huge outliers, and only slowly
widen the model. So burn in fast, then replay at production rates.

```bash
cd src/cloud-check && source .venv/bin/activate
# pass 1 — burn-in, fast EMA, saves the converged bank
python bird_pipeline.py --from-frame 500 --no-render \
  --alpha 0.40 --stat-alpha 0.30 --save-seed bird_seed.npz
# pass 2 — production replay from the seed, with the audit gallery
python bird_pipeline.py --from-frame 500 --load-seed bird_seed.npz \
  --render interesting
```

`--sweep` calibrates `cd_z` / `ad_z` / `min_area` against the RTC negative set
instead of asserting a sigma (see "thresholds must be calibrated" below).
`--gallery-only` rebuilds the HTML from an existing `index.csv`.

Output in `bird_pipeline_out/`: 9-panel PNG per frame, `index.csv`,
`gallery.html` (filters for **missed birds** and **RTC false positives** — the
two views that actually matter).

## Camera registration — MEASURED (2026-09-05), `align_probe.py`

The worry was that occasional few-cm camera moves invalidate a per-pixel model.
Phase correlation on high-passed luma across all 2182 RTC frames says:

| consecutive 15-min RTC step | shift |
|---|---|
| median | **0.042 px** |
| p90 | 0.17 px |
| p99 | 0.90 px |
| > 1 px | 0.72 % |

Within-day cumulative drift ≈ +0.14 px/day — the camera does **not** wander, so
per-pixel modelling needs no routine registration. Real bumps are rare and
discrete: **exactly 2 in 47 days** (#1023→#1028 and #1059→#1062, ~10 px each).
So the answer is *detect the bump and rebuild the model*, not warp every frame.

Unresolved: across a 24 h gap the median shift is 1.7–2.4 px, not 0.04. But the
dawn / midday / dusk anchors disagree on the total displacement over the same
period (−50 / −28 / −37 px), which a real physical drift could not do — so at
least part of that is registration being fooled by genuine scene change
(weather, plant growth, objects added). `BUMP_PX` defaults to 6 px to sit above
that artifact and below a real bump.

## Thresholds must be calibrated, not asserted

With ~480k pixels per frame, "6 sigma" is meaningless: even a perfect Gaussian
puts thousands of pixels past it, and the real residual is far heavier-tailed —
measured `z_cd_max` p50 = **10.9 on frames that are pure background**. The RTC
set is 2189 near-certain negatives, so the operating point should be *chosen*
from a recall-vs-false-positive curve (`--sweep`), not hand-picked.

## DECISIVE MEASUREMENT — `model_probe.py` (2026-09-05)

Asks the prior question with **no detector in the loop**: how well can we
predict the background at all? Three references compared over 505 frames
(437 RTC negatives + 68 labelled birds).

### Metric A — background prediction error (detector-free)
median `|frame − affine-fitted reference|`, DN:

| reference | RTC p50 | RTC p90 |
|---|---:|---:|
| previous RTC frame | **5.6** | 12.9 |
| knn (median of 5 lighting-matched past RTC frames) | 9.4 | 22.9 |
| EMA (what both pipelines used) | **22.0** | 36.0 |

**The EMA is the worst possible choice** — 4× worse than simply using the last
RTC frame, and its 22 DN error sits at the bird-signal scale, destroying the
margin before any detector runs. This explains why two pipelines failed.

### Metric B — separability, AUC bird vs RTC (0.5 = coin flip)

| reference | cd_max | ad_max | adocc_max | cd_blob | ad_blob |
|---|---:|---:|---:|---:|---:|
| prev | **0.712** | 0.710 | 0.681 | 0.623 | 0.710 |
| knn | 0.606 | 0.596 | 0.588 | 0.606 | 0.598 |
| EMA | 0.538 | 0.449 | 0.446 | 0.469 | 0.447 |

EMA scores at or below chance. Best is 0.71 — real, but not usable.

### The overlap, which is what actually matters
Peak response per frame, prev reference:

| | RTC p50 | RTC p90 | RTC p99 | BIRD p50 | BIRD p90 |
|---|---:|---:|---:|---:|---:|
| cd_max | 7.17 | 12.36 | 24.57 | 10.06 | 17.00 |

- threshold passing 10 % of RTC frames → **bird recall 32 %**
- threshold passing 1 % of RTC frames → **bird recall 6 %**

The bird median (10.1) sits **below** the background p90 (12.4). Massive overlap.

### Two honest corrections this forced
1. **Metric A's 5.6 DN is a median, and detection is a tail problem.** The bulk
   of the scene is highly predictable; the worst few pixels per frame are not,
   and those are what a bird must out-score. A median-vs-peak comparison
   flatters the result — do not quote the 5.6 DN as a detection margin.
2. **Blob-shape scoring did not help** (0.710 → 0.710 on `ad`, and *worse* on
   `cd`), and the **structure-occlusion gate made things slightly worse**
   (0.710 → 0.681). Both were expected to help. They did not.

### What this does and does not prove
Proven: frame-level peak statistics on a background-difference, with these
cues, do **not** separate birds here — no threshold gives usable recall.

NOT proven: that the scene is unsolvable. Every metric above is a frame-level
proxy that conflates *"a bird is present"* with *"the bird is the most
anomalous thing in the frame"*. A bird is ~100 of 76 800 px, so it competes
against every leaf, glint and registration error anywhere in the image.

**The missing ingredient is bounding boxes.** With 87 labelled bird *frames*
but no locations, the actual signal-to-noise at the bird cannot be measured.
That single gap blocks any further honest evaluation of this approach.

## Grouping / retrieval — `group_probe.py` + retrieval in `model_probe.py`

"Group the RTC frames by time and cloud conditions, build a model per group."
Measured over 1996 RTC frames, train/test split, detector-free prediction error:

| reference | error |
|---|---:|
| EMA (both failed pipelines) | 22.0 DN |
| global median (K=1) | 12.48 |
| clock-hour groups, K=128 | 12.40 |
| solar elevation+azimuth, K=128 | 11.65 |
| solar + cloud, K=128 | 10.01 |
| previous RTC frame | 5.94 |
| **appearance retrieval, causal** | **4.21** |

Solar geometry was expected to dominate (shadow position is deterministic from
the sun) - it barely helps, and clock time does nothing. Averaging hurts
monotonically (n=1 3.32 < n=3 3.35 < n=5 3.54 < n=15 4.25 << EMA 22.0).
Retrieval is genuine lighting matching, not temporal adjacency: only 40% of
matches lie within an hour, 27% are more than a day away.

### THE TRAP: a better model made detection WORSE

| reference | prediction error | AUC (cd_max) | recall @10% RTC FP |
|---|---:|---:|---:|
| previous RTC frame | 5.6 DN | **0.712** | **32%** |
| appearance retrieval | **3.8 DN** | 0.591 | 9% |

Retrieval wins the model metric decisively and **loses detection by 3.5x**.

Cause: `pred_err` measures **luma** (affine-normalised grey), and the retrieval
features are PCA over **greyscale** thumbnails - but the detector's strongest
cue is **CD, a chroma statistic**. Retrieval therefore selects frames matched in
brightness structure while free to differ in white balance, and every AWB
mismatch shows up as chroma distortion across the whole frame. Background
`cd_max` p50 rises 7.17 -> 9.50 even as luma error falls 5.6 -> 3.8.

**Methodological lesson: the reference was selected with a proxy metric not
aligned to the objective.** Optimising Metric A actively damaged Metric B.
Any future reference search must be scored on the detector's own statistic.

### Where background subtraction actually tops out
Best configuration found anywhere in this work: previous RTC frame + `cd_max`,
**32% bird recall at a threshold passing 10% of RTC frames** (6% at 1%).
Against a hard requirement of recall = 1.0, that is not deployable.

## THE ESP32 PRE-FILTER IS SAVEABLE - by inverting the question

Detecting a bird on-device is hopeless (~100 anomalous px in 480k). But
recognising that a trigger was **explained by a lighting event** is a large,
global, low-frequency signal, and it is suppression-only, so it cannot cost
recall: a pigeon does not change the whole frame.

Device telemetry was deliberately NOT used - `tile_means` etc. come from the
old firmware across months of changing ETTR/AWB behaviour. All features below
are recomputed from the JPGs exactly as new firmware would compute them
(`presuppress_probe.py`).

### Timing is the strongest signal found anywhere in this work
Rapid cloud changes re-trigger the PIR; a long quiet spell means something
actually arrived. Bird rate against quiet-time before the trigger:

| quiet before trigger | frames | birds | bird rate |
|---|---:|---:|---:|
| 0-30 s | 497 | 6 | **1.21%** |
| 1-5 min | 427 | 14 | 3.28% |
| 1-3 h | 150 | 11 | 7.33% |
| > 3 h | 83 | 9 | **10.84%** |

A **9x** swing, from a feature that costs nothing - the RTC already knows the
time of the last event. Position within a rapid burst (<60 s apart) is just as
clean: position 0 = 4.77% birds, position 4 = **0%**, position >=5 = **0%**
(118 frames, not one bird).

### The rule, and what it buys
Suppress when `quiet < G` **or** `burst_position >= P` **or** `res_p95 > R`,
where `res_p95` is the 95th-percentile tile residual after a global
illumination fit `a*prev + b` against the previous frame.

| G | P | R | PIR suppressed | birds lost | margin |
|---:|---:|---:|---:|---:|---|
| 15 s | 4 | 60 | **24.2%** | 0/68 | R has NO margin (bird max 60.1) |
| 15 s | 5 | 72 | 17.6% | 0/68 | ~20% on all three |
| 15 s | 4 | off | 11.8% | 0/68 | **no image processing at all** |
| 10 s | 6 | 90 | 10.7% | 0/68 | large margins |

Recommended: **G=15 s, P=5, R=72 -> 17.6%** at zero bird loss with real margin
on every threshold. The temporal-only variant (11.8%) needs no pixels at all
and is the cheapest defensible thing the firmware could do.

### Caveat that decides how aggressively to set it
Thresholds are fitted to 68 labelled birds. Setting a bound exactly at the most
extreme observed bird (the 24.2% row) has zero margin and will break on the
first bird outside the range. Suppression here removes an *upload*, not a
wake-up - the WiFi transmit is the dominant battery cost, so 17.6% fewer
uploads is a real saving, but this is a modest win, not a solution to the
99%-empty problem.

## Open items / next steps

- **Priority: build the Horprasert alpha/CD + structure-occlusion detector** (see
  the shadow-removal section above). This subsumes the earlier "blob shape filtering"
  idea and removes the harsh plant/edge masks. Awaiting go-ahead to wire into
  `regime_diff.py` and re-render the #2677–2710 benchmark + the 6 bird frames.
- Plant/edge masks weakest in high-contrast sunny regimes — most residual left.
- Other levers: sub-pixel registration before diff; per-regime blob threshold;
  lean more on chroma for the grey-pigeon case.
- This is **server-side only** (full-res, colour, per-pixel) — it cannot run on
  the battery-limited ESP32, which keeps doing the coarse tile pre-filter.
</content>
</invoke>
