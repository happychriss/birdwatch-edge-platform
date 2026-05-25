from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DATASET_ROOT = Path(__file__).resolve().parents[3] / "training-data"
SYNTH_ROOT = Path(__file__).resolve().parents[1] / "synth-data"

# Maps source folder → (binary label, domain tag).
# "clouds" = empty scene under some lighting (the false-positive class to suppress).
# "process" = anything-new in the frame (bird, person, simulated object).
FOLDERS = {
    "ignore-sun_shining":     ("clouds",  "real-2026"),
    "process-birds-pillow":   ("process", "real-2026"),
    "process-people":         ("process", "real-2026"),
    "process-real-birds":     ("process", "real-2026"),
    "process-dark":           ("process", "real-2026"),
}

SYNTH_FOLDERS = {
    "lighting": ("clouds", "synth"),
    "birds_paste": ("process", "synth"),
}

# Folders where the label is encoded in the filename prefix (prefix_YYYYMMDD_HHMMSS.jpg).
# Used for ad-hoc test scenarios where mixed labels land in a single folder.
FILENAME_LABELED_FOLDERS = {
    "real-data/wrong_night": "real-2026",
}
_FILENAME_LABEL_PREFIX = {
    "cloud":  "clouds",
    "person": "process",
    "pillow": "process",
}

_TIMESTAMP_RE = re.compile(r"(20\d{6})_(\d{6})")


@dataclass
class Sample:
    """A labelled training frame.

    `source` mirrors the ESP wakeup-source telemetry. Offline corpora have no
    real source attribute — sweep tools can synthesize an RTC schedule by
    setting `sample.source = 'rtc'` on the desired subset (the model only
    updates on rtc frames, matching the on-device gate).
    """
    path: Path
    label: str                   # "clouds" or "process"
    domain: str                  # "real-2026" or "synth"
    taken_at: datetime | None
    source: str = "pir"          # "rtc" (reference, updates the model) or "pir" (evidence only)

    @property
    def hour_bucket(self) -> int:
        """Raw hour-of-day (0..23). Kept for legacy analysis scripts."""
        return self.taken_at.hour if self.taken_at else 12


def _parse_timestamp(name: str) -> datetime | None:
    m = _TIMESTAMP_RE.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(0), "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def load_dataset(root: Path | None = None, include_synth: bool = False) -> list[Sample]:
    base = (root or DATASET_ROOT).resolve()
    samples: list[Sample] = []
    for rel, (label, domain) in FOLDERS.items():
        folder = base / rel
        if not folder.is_dir():
            continue
        for jpg in sorted(folder.glob("*.jpg")):
            samples.append(
                Sample(
                    path=jpg,
                    label=label,
                    domain=domain,
                    taken_at=_parse_timestamp(jpg.name),
                )
            )
    for rel, domain in FILENAME_LABELED_FOLDERS.items():
        folder = base / rel
        if not folder.is_dir():
            continue
        for jpg in sorted(folder.glob("*.jpg")):
            prefix = jpg.stem.split("_")[0]
            label = _FILENAME_LABEL_PREFIX.get(prefix)
            if label is None:
                continue
            samples.append(
                Sample(path=jpg, label=label, domain=domain,
                       taken_at=_parse_timestamp(jpg.name))
            )

    if include_synth and SYNTH_ROOT.is_dir():
        for rel, (label, domain) in SYNTH_FOLDERS.items():
            folder = SYNTH_ROOT / rel
            if not folder.is_dir():
                continue
            for jpg in sorted(folder.glob("*.jpg")):
                samples.append(
                    Sample(
                        path=jpg,
                        label=label,
                        domain=domain,
                        taken_at=_parse_timestamp(jpg.name),
                    )
                )
    return samples
