"""Online-style evaluation: feed frames in chronological order, update the
background model only on RTC-tagged frames, score each prediction against
ground truth.

This mirrors the firmware rule exactly: PIR frames are pure evidence and never
fold back into the model; only RTC reference frames refresh the per-cell EMA.
The reported numbers therefore include the cold-start period (early frames
lean toward 'process' on purpose).

Offline corpora carry no real `source` field — the sweep tooling synthesizes
an RTC schedule by setting `sample.source = 'rtc'` on a chronological subset
(e.g. 1-in-4). For DB-driven runs `Sample.source` is filled from
`meta['source']` so the simulation matches production exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

from .background import BackgroundModel, photo_bucket_idx
from .classifier import ClassifierResult, classify
from .config import Config
from .dataset import Sample
from .features import extract_tile_features_yuv, load_yuv_vga


@dataclass
class StreamResult:
    sample: Sample
    pred: ClassifierResult
    correct: bool


def run_stream(
    samples: Iterable[Sample],
    cfg: Config | None = None,
    update_only_on_cloud_prediction: bool = True,
) -> list[StreamResult]:
    """Process samples in iteration order. Sort upstream by timestamp for a
    chronological run.

    Model update policy (mirrors the on-device rule):
      • PIR frames never update the model — pure evidence.
      • RTC frames update the model when prediction warrants it
        (warmup, QUIET, SCENE_DRIFT, NIGHT).
      • If `update_only_on_cloud_prediction` is False, the policy becomes
        "update only on ground-truth cloud frames" — kept for oracle replays
        in the validator.
    """

    cfg = cfg or Config()
    model = BackgroundModel(cfg)
    out: list[StreamResult] = []
    # prev tile means per (photo_bucket, scene_bucket) cell, in Y/U/V triples
    prev_state: dict[tuple[int, int], tuple[np.ndarray, np.ndarray, np.ndarray]] = {}

    for s in samples:
        y, u, v = load_yuv_vga(s.path)
        feats = extract_tile_features_yuv(y, u, v)
        gm = feats["global_mean"]
        pb_name = model.photo_bucket_for(gm)
        pb = photo_bucket_idx(pb_name)
        sb = model.scene_bucket_for(pb, feats["mean_y"])
        prev = prev_state.get((pb, sb))
        prev_y = prev[0] if prev is not None else None

        # Observe before classifying so warmup counter reflects the model
        # that was used for prediction (matches on-device behaviour).
        was_warmup = model.warmup_remaining(pb, sb) > 0
        model.observe(pb, sb)

        pred = classify(
            feats["mean_y"], model, cfg,
            prev_tile_mean=prev_y,
            tile_mean_u=feats["mean_u"],
            tile_mean_v=feats["mean_v"],
        )

        # Update policy:
        #  • update_only_on_cloud_prediction=True  → mirror on-device
        #  • update_only_on_cloud_prediction=False → oracle (label==clouds)
        if update_only_on_cloud_prediction:
            # RTC-only gate: PIR frames never update. RTC frames update when
            # prediction agrees the scene is empty / drifting / dark.
            if s.source == "rtc" and (
                was_warmup
                or pred.label == "clouds"
                or pred.trigger in ("SCENE_DRIFT", "NIGHT")
            ):
                model.update(pb, sb, feats["mean_y"], feats["mean_u"], feats["mean_v"])
            if pred.trigger == "SCENE_DRIFT" and s.source == "rtc":
                model.reset_warmup(pb, sb)
        else:
            if s.label == "clouds":
                model.update(pb, sb, feats["mean_y"], feats["mean_u"], feats["mean_v"])

        prev_state[(pb, sb)] = (feats["mean_y"], feats["mean_u"], feats["mean_v"])
        out.append(StreamResult(sample=s, pred=pred, correct=(pred.label == s.label)))

    return out


def confusion(results: list[StreamResult]) -> dict:
    tp = sum(1 for r in results if r.sample.label == "process" and r.pred.label == "process")
    fn = sum(1 for r in results if r.sample.label == "process" and r.pred.label == "clouds")
    fp = sum(1 for r in results if r.sample.label == "clouds"  and r.pred.label == "process")
    tn = sum(1 for r in results if r.sample.label == "clouds"  and r.pred.label == "clouds")
    total = len(results)
    correct = tp + tn
    return {
        "tp_process_correct": tp,
        "fn_missed_bird_or_person": fn,
        "fp_clouds_uploaded": fp,
        "tn_clouds_filtered": tn,
        "accuracy": correct / total if total else 0.0,
        "process_recall": tp / (tp + fn) if (tp + fn) else 0.0,
        "clouds_recall": tn / (tn + fp) if (tn + fp) else 0.0,
    }


def write_csv(results: list[StreamResult], path: Path) -> None:
    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "filename", "domain", "source", "label", "pred", "trigger",
            "photo_bucket", "scene_bucket",
            "blob_max", "anomaly_ratio", "compactness", "warmup",
            "new_dark_tiles", "dark_tiles", "n_chroma_changed", "chroma_delta_max",
            "temporal_available", "reason", "correct",
        ])
        for r in results:
            w.writerow([
                r.sample.path.name,
                r.sample.domain,
                r.sample.source,
                r.sample.label,
                r.pred.label,
                r.pred.trigger,
                r.pred.photo_bucket,
                r.pred.scene_bucket,
                r.pred.blob_max_size,
                f"{r.pred.anomaly_ratio:.3f}",
                f"{r.pred.compactness:.3f}",
                int(r.pred.warmup),
                r.pred.new_dark_tiles,
                r.pred.dark_tiles,
                r.pred.n_chroma_changed,
                f"{r.pred.chroma_delta_max:.2f}",
                int(r.pred.temporal_available),
                r.pred.reason,
                int(r.correct),
            ])
