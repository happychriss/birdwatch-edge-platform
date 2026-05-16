"""Online-style evaluation: feed frames in chronological order, update the
background model only on accepted-as-cloud frames, score each prediction
against the ground truth.

This mirrors how the firmware will operate over time, so the reported numbers
include the cold-start period (early frames lean toward 'process' on purpose).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

from .background import BackgroundModel
from .classifier import ClassifierResult, classify
from .config import Config
from .dataset import Sample
from .features import extract_tile_features, load_gray_vga


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
    """Process samples in iteration order. Always sort by timestamp upstream
    if you want a chronological run."""

    cfg = cfg or Config()
    model = BackgroundModel(cfg)
    out: list[StreamResult] = []
    prev_tile_mean: dict[int, np.ndarray] = {}  # bucket_idx → last tile_mean for that bucket

    for s in samples:
        frame = load_gray_vga(s.path)
        feats = extract_tile_features(frame)
        bucket = model._idx(s.hour_bucket)
        prev = prev_tile_mean.get(bucket)

        # Snapshot the warmup state BEFORE we observe this frame — we want
        # the predicate to reflect the model that was used for prediction.
        was_warmup = model.warmup_remaining(s.hour_bucket) > 0
        # Count every observation toward the bucket's warmup, even before we
        # have a verdict — otherwise low-traffic buckets stay in warmup forever.
        model.observe(s.hour_bucket)
        pred = classify(feats["mean"], s.hour_bucket, model, cfg, prev_tile_mean=prev)

        # Background update policy:
        # - oracle mode: update from ground-truth cloud frames only.
        # - online mode:
        #     warmup: fold every frame in (bootstrap).
        #     cloud prediction: fold in (it looks like background).
        #     SCENE_DRIFT: dark tiles already in prev frame → stale model → update.
        #     NIGHT / INDIRECT_LIGHT: upload unconditionally but still fold in so
        #       the model tracks the dark-scene / side-light baseline (matches C).
        if update_only_on_cloud_prediction:
            if was_warmup or pred.label == "clouds" or pred.trigger in (
                "SCENE_DRIFT", "NIGHT", "INDIRECT_LIGHT"
            ):
                model.update(s.hour_bucket, feats["mean"])
            if pred.trigger == "SCENE_DRIFT":
                model.reset_warmup(s.hour_bucket)
        else:
            if s.label == "clouds":
                model.update(s.hour_bucket, feats["mean"])

        prev_tile_mean[bucket] = feats["mean"]  # always record last frame per bucket

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
            "filename", "domain", "hour", "label", "pred", "trigger",
            "blob_max", "anomaly_ratio", "compactness", "warmup",
            "new_dark_tiles", "temporal_available", "reason", "correct",
        ])
        for r in results:
            w.writerow([
                r.sample.path.name,
                r.sample.domain,
                r.sample.hour_bucket,
                r.sample.label,
                r.pred.label,
                r.pred.trigger,
                r.pred.blob_max_size,
                f"{r.pred.anomaly_ratio:.3f}",
                f"{r.pred.compactness:.3f}",
                int(r.pred.warmup),
                r.pred.new_dark_tiles,
                int(r.pred.temporal_available),
                r.pred.reason,
                int(r.correct),
            ])
