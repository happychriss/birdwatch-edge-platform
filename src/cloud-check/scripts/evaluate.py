"""End-to-end evaluation on the labelled training set.

Usage (from /workspace/cloud-check):
    .venv/bin/python -m scripts.evaluate
    .venv/bin/python -m scripts.evaluate --skip-aux       # drop with-birds/
    .venv/bin/python -m scripts.evaluate --oracle-update  # update bg with ground truth (upper bound)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the package importable when run as `python scripts/evaluate.py` too.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_check.config import Config
from cloud_check.dataset import load_dataset
from cloud_check.pipeline import confusion, run_stream, write_csv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-aux", action="store_true", help="Drop the 2025 with-birds aux set")
    ap.add_argument("--with-synth", action="store_true",
                    help="Include synth-data/ augmentations alongside real data")
    ap.add_argument("--oracle-update", action="store_true",
                    help="Update background using ground-truth labels (upper-bound baseline)")
    ap.add_argument("--report-dir", type=Path,
                    default=Path(__file__).resolve().parents[1] / "reports")
    args = ap.parse_args()

    samples = load_dataset(include_synth=args.with_synth)
    if args.skip_aux:
        samples = [s for s in samples if s.domain != "aux-2025"]

    samples = sorted(
        samples,
        key=lambda s: (s.taken_at.isoformat() if s.taken_at else s.path.name),
    )

    cfg = Config()
    results = run_stream(samples, cfg, update_only_on_cloud_prediction=not args.oracle_update)
    cm = confusion(results)

    print(f"\n--- Configuration ---")
    print(f"  samples       : {len(results)}")
    print(f"  oracle-update : {args.oracle_update}")
    print(f"  skip-aux      : {args.skip_aux}")
    print(f"  num_time_buckets      = {cfg.num_time_buckets}")
    print(f"  tile_z_threshold      = {cfg.tile_z_threshold}")
    print(f"  quiet_anomaly_ratio   = {cfg.quiet_anomaly_ratio}")
    print(f"  dark_object_min_delta = {cfg.dark_object_min_delta}")
    print(f"  dark_object_min_tiles = {cfg.dark_object_min_tiles}")
    print(f"  temporal_dark_delta   = {cfg.temporal_dark_delta}")
    print(f"  ema_alpha             = {cfg.ema_alpha}")
    print(f"  warmup_frames/bucket  = {cfg.warmup_frames_per_bucket}")

    print(f"\n--- Per-domain breakdown ---")
    domains = sorted({r.sample.domain for r in results})
    for d in domains:
        sub = [r for r in results if r.sample.domain == d]
        print(f"  {d:12s}  n={len(sub):3d}  acc={sum(r.correct for r in sub)/max(len(sub),1):.3f}")

    print(f"\n--- Confusion ---")
    print(f"  bird/person correctly uploaded   (TP) : {cm['tp_non_cloud_correct']}")
    print(f"  bird/person MISSED as cloud      (FN) : {cm['fn_missed_bird_or_person']}")
    print(f"  cloud spuriously uploaded        (FP) : {cm['fp_cloud_uploaded']}")
    print(f"  cloud correctly filtered         (TN) : {cm['tn_cloud_filtered']}")
    print(f"  accuracy             : {cm['accuracy']:.3f}")
    print(f"  non-cloud recall     : {cm['non_cloud_recall']:.3f}   (1.0 = never miss a bird)")
    print(f"  cloud recall         : {cm['cloud_recall']:.3f}   (1.0 = perfectly suppress clouds)")

    args.report_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.report_dir / ("eval_oracle.csv" if args.oracle_update else "eval_online.csv")
    write_csv(results, csv_path)
    json_path = csv_path.with_suffix(".json")
    json_path.write_text(json.dumps({"confusion": cm, "n": len(results)}, indent=2))
    print(f"\nWrote {csv_path}")
    print(f"Wrote {json_path}")


if __name__ == "__main__":
    main()
