# BirdWatch Detection Algorithm — Learnings & Findings

*Accumulated from development sessions through 2026-05-29. Written to be
readable without reading the code — technical terms are explained inline.*

---

## 1. The core problem

A PIR (Passive Infrared) sensor fires on any rapid infrared change in its field
of view — not only a moving warm animal. Clouds passing in front of the sun cause
almost identical scene changes. The goal is to tell the difference and suppress
the cloud false-triggers while never missing a real bird.

### Key constraint
**Recall = 1.0 is a hard requirement.** Missing a bird is worse than sending an
empty photo. The system is explicitly designed to over-send rather than under-send.
This frames every threshold and design choice: when in doubt, upload.

---

## 2. The signal: what makes a bird detectable

The camera sees a fixed balcony scene from a low angle. In this geometry:

- **Dark pigeon against bright sky or bright floor** — by far the most common case.
  The bird creates a compact, locally-dark region (1–5 tiles out of 300).
- **Pigeon on railing against sky** — same, but smaller: often 1–2 tiles.
- **Pigeon on dark background (plant, pot)** — the bird is sometimes *brighter*
  than the locally-dark background model. Weak signal on the luma channel.

All labelled birds in the dataset (2026-05-21 to 2026-05-29) were detected as
**dark anomalies against the background model**. A bright-blob detector
(tile lighter than model) adds no recall and 73% false-positive rate — sun
glints and sky clearing look the same. **The dark-only logic is correct.**

---

## 3. The background model

### What it is
A per-tile running average of the scene. Each tile (8×8 pixels, 20×15 grid =
300 tiles) stores a mean Y (luma) value updated via **EMA (Exponential Moving
Average)** with α=0.15. Only **RTC frames** (the scheduled 15-min reference
shots) update the model. PIR frames never modify it — a bird landing cannot
corrupt the background.

### Why it matters
The model is the reference against which current tiles are compared. A tile 20+
DN darker than its model value is a "dark anomaly." Without a good model, you
get either floods of false anomalies (model too bright) or missed birds (model
too dark).

### The warmup problem
When the device cold-starts, the model has no data. For the first ~4 RTC frames
(≈1 hour), the model is in "warmup" and every frame is uploaded regardless. This
is safe but means the first hour can over-send. It also means frames captured
before the model converges are unreliable for algorithm evaluation.

**Key finding:** Frames 326–331 in the dataset (May 22) are all warmup — the
model wasn't trained yet. Evaluating algorithm performance on pre-warmup frames
gives misleading results. Reliable evaluation starts from frame ~511 onwards,
when the 15-min RTC cycle began.

---

## 4. Illumination: the central challenge

### What goes wrong
Sunlight changes throughout the day and with cloud cover. If you compare a
tile's brightness to a model trained at a different lighting level, you get
widespread false anomalies — the whole scene looks "dark" during dusk even
though nothing changed structurally.

### Approach 1 — Global affine normalization (current production)
Per-frame, fit a linear equation `predicted[i] = a × model[i] + b` over all
tiles. This single (a, b) pair describes the global brightness shift. Subtract
it before comparing: the residual is illumination-invariant.

**Works well when:** The whole scene shifts uniformly (overcast sky, morning
light).

**Fails when:** Sky and floor dim at different rates (evening, direct sun at
low angle). One linear fit can't describe two different slopes simultaneously.

### Approach 2 — Spatial high-pass filter (experimental, 2026-05-29)
Instead of fitting one global line, apply a box-blur to the model and to the
current frame, then subtract:

```
HP_delta[i] = (model[i] − blur(model)[i]) − (tile[i] − blur(tile)[i])
```

`blur` averages each tile with its 5×5 neighbourhood (25 tiles). The blurred
version captures slow illumination gradients. Subtracting it leaves only
local structure — compact dark objects.

**Works well for:** Evening foreground-dimming, diffuse cloud shadows, any
scene where illumination varies smoothly across the image.

**Fails on / caveats:**
- Hard shadow edges are high-frequency and survive the high-pass. A tree
  shadow sweeping across a tile boundary looks identical to a small bird.
- Structural scene elements (plant edges, pot outlines, railing corners)
  always produce a local HP contrast. The empty-scene HP noise floor is
  ~20–25 DN — the same order as the bird signal.
- Reference staleness: even a 15-min-old model drifts ~34 DN median HP.
  The bird signal is 28–37 DN. The margin is thin.

### The hard ceiling
At 100% bird recall, the current dataset allows ~22% suppression of non-bird
frames after DUPLICATE is handled. This is a **data limitation, not an
algorithm limitation**. The two weakest bird frames score 28.6 and 29.7 DN in
HP space. The structural noise floor sits at 25–30 DN. With only 5 bird
examples there is not enough statistical margin to push the threshold higher
without risking a miss.

**What this means in practice:** More labelled bird frames (target 20–30)
would clarify the true lower bound of the bird signal and allow a defensible
higher threshold.

---

## 5. The blob detector

### What a "blob" is
After computing which tiles are anomalously dark, the connected-component
algorithm groups adjacent dark tiles into blobs. "8-connected" means diagonal
neighbours count. A pigeon on the floor typically occupies 2–5 tiles (at
8×8 px per tile, a pigeon spans ~30–40 px). A diffuse cloud shadow spans
60–150 tiles.

### The old DARK_OBJ (pre-2026-05-29)
Required both:
- Tile dark vs model (`dark_model_tiles ≥ 1`)
- Tile newly dark vs previous frame (`new_dark_tiles ≥ 1`)

**Fatal flaw:** A pigeon that sits still for two frames has zero `new_dark_tiles`
— it looks identical to the previous frame. This suppressed real birds.

### The new DARK_BLOB (Python, post-2026-05-29)
Requires:
- `dark_tiles ≥ 1` (dark vs model)
- `dark_blob_max ≤ 5` (blob is bird-sized, not a cloud shadow)

No frame-to-frame requirement. A motionless pigeon is correctly detected.

**Known failure mode:** Blobs of 6+ tiles fall through to AMBIGUOUS
(which is a process/upload). But the blob cap (≤ 5) itself caused the 3
missed birds in the dataset (frames 557, 558, 614) where the evening
foreground-dimming merged the bird blob with surrounding dark tiles → blob
grew to 7–15 → cap fired → QUIET suppressed. The HP pipeline removes the
cap because HP already strips the diffuse background.

---

## 6. The burst pre-filter (Layer-1)

Runs before the background model. Compares the current PIR frame to the
immediately-previous captured frame.

**What it catches (cheaply, no model needed):**
- `DUPLICATE` — pixel-identical re-fires (PIR triplets). Both luma AND chroma
  must be unchanged; a pigeon shifts chroma even if luma saturates.
- `NIGHT` — scene too dark (gm < 70) for reliable detection.

**What it cannot catch reliably:**
- A pigeon that just arrived — looks different from previous, correctly passes.
- A pigeon that has been sitting — looks the same as previous, DUPLICATE would
  fire. The chroma gate saves this: a pigeon's plumage has different colour
  from sky/floor background.

**Old stages now removed or weakened:**
`BRIGHTNESS_SHIFT`, `FAST_SHIFT`, `BRIGHT_STABLE`, `DIFFUSE` were all
luma-heuristic suppression rules that the HP Layer-2 handles more robustly.
In the simplified pipeline they pass to Layer-2 instead of suppressing.

---

## 7. The RTC reference cadence

The device wakes on a 15-minute timer to take "reference" photos that update
the background model. Key measurements (May 2026 dataset, 210 RTC frames):

| Time gap | Scene HP-drift median | vs bird signal (~30 DN) |
|---|---|---|
| 15 min | 34 DN | at the signal level |
| 30 min | 44 DN | slightly worse |
| 60 min | 56 DN | noticeably worse |

**But the drift is highly time-of-day dependent:**
- Dawn (05–06h): 18–23 DN — model stays fresh well beyond 30 min
- Midday sun (07–15h): 47–89 DN — even 15 min is not enough; shadows sweep too fast
- Evening (16–18h): 16–20 DN — very stable, 30+ min intervals work fine

**Conclusion:** The 15-min cadence is primarily useful during midday direct sun.
In flat light (dawn/dusk/overcast), 30–45 min would give nearly identical
detection quality. If moving to 30 min, increase EMA alpha from 0.15 to ~0.30
to maintain the same wall-clock model responsiveness. For now, keep 15 min
during the data-collection phase; re-evaluate once the bird dataset is larger.

---

## 8. Chroma as a parallel detection channel

YCbCr is the colour space used. Y = luma (brightness). Cb (U) and Cr (V)
encode colour — blue-yellow and red-green directions. A pigeon is a neutral
grey-brown, quite different from a sky tile (blue-shifted) or bright floor
(neutral but much brighter).

Chroma anomaly = tile's U/V differ from what the model expects at that position,
after subtracting the median shift across the frame (to absorb global sunset
colour shifts). Threshold: ΔC² > 64 (ΔC ≈ 8 DN linear distance in UV space).

**Key finding:** The chroma gate was the critical fix for the pigeon-at-noon
case (frames 517/518): a pigeon at a saturated scene has zero Y change (luma
identical to previous frame) but visible chroma shift (pigeon colour ≠ sky
colour) → chroma saves DUPLICATE from firing.

**Chroma is direction-agnostic** — it fires whether the bird is darker or
lighter than the background. If pale-coloured birds appear, chroma is the right
channel to lean on; bright-luma detection is not.

---

## 9. Texture as a third signal

Per-tile Y standard deviation (std_y) measures how "patchy" the tile is. Smooth
sky = low std_y. Bird plumage = medium-high std_y. Used as a loose secondary
gate: a tile with high texture AND loosely dark vs model triggers even if the
absolute dark delta is below threshold.

Current config: `texture_min_std_y = 12 DN`, requires tiles also darker than
model by ≥ 10 DN. This channel is available in Python only (std_y is decoded
from JPEG); not yet in the ESP32 firmware.

---

## 10. What does not work — honest assessment

| Approach | Why it fails here |
|---|---|
| Bright-blob detection | Sun glints, sky clearing produce identical HP bright peaks. 73% FP rate; no additional recall. |
| Frame-difference as primary detector | Pigeons sit still. Bird is invisible to any diff-based rule after the first frame. |
| Faster EMA (larger α) to track light | Learns a sitting bird into background; the bird disappears after 2–3 RTC frames. |
| Global affine normalization alone | Can't handle sky-vs-foreground slope mismatch at evening; this is what caused the 3 missed birds. |
| Higher HP threshold for more suppression | Lowest bird score (28.6 DN) overlaps structural noise floor. Ceiling without more data: ~22% suppression at 100% recall. |
| Blob size cap in HP space | HP already removes diffuse background; the cap excludes nothing bad and excludes birds merged with plant-edge blobs. Removed in HP pipeline. |
| Motion/trajectory detection | Pigeons dominant behaviour is sitting. No temporal coherence to exploit. |

---

## 11. Open work

1. **ESP32 firmware**: still runs old DARK_OBJ pipeline. Needs DARK_BLOB port
   (remove `new_dark_tiles`, add connected-component blob size check). The HP
   pipeline remains Python-only experiment for now.
2. **Threshold calibration**: needs 20+ labelled bird frames to establish the
   true lower bound of the bird HP score distribution.
3. **Cadence decision**: dense 15-min collection ongoing; re-evaluate for 30-min
   production once dataset is larger.
4. **Pale-bird case**: no examples in current dataset. If a light-coloured bird
   appears, it will not be detected by dark-luma but should be caught by chroma.
   The system will over-send (AMBIGUOUS) rather than miss it.
