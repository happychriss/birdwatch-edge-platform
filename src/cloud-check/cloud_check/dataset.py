from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DATASET_ROOT = Path(__file__).resolve().parents[3] / "training-data"
SYNTH_ROOT = Path(__file__).resolve().parents[1] / "synth-data"

# Maps source folder → (binary label, domain tag).
# "cloud" = empty scene under some lighting (the false-positive class to suppress).
# "non-cloud" = anything-new in the frame (bird, person, simulated object).
FOLDERS = {
    "real-data/clouds":               ("cloud",     "real-2026"),
    "real-data/process-birds-pillow": ("non-cloud", "real-2026"),
    "real-data/process-people":       ("non-cloud", "real-2026"),
}

SYNTH_FOLDERS = {
    "lighting": ("cloud", "synth"),
    "birds_paste": ("non-cloud", "synth"),
}

# Folders where the label is encoded in the filename prefix (prefix_YYYYMMDD_HHMMSS.jpg).
# Used for ad-hoc test scenarios where mixed labels land in a single folder.
FILENAME_LABELED_FOLDERS = {
    "real-data/wrong_night": "real-2026",
}
_FILENAME_LABEL_PREFIX = {
    "cloud":  "cloud",
    "person": "non-cloud",
    "pillow": "non-cloud",
}

_TIMESTAMP_RE = re.compile(r"(20\d{6})_(\d{6})")


@dataclass(frozen=True)
class Sample:
    path: Path
    label: str           # "cloud" or "non-cloud"
    domain: str          # "real-2026" or "aux-2025"
    taken_at: datetime | None

    @property
    def hour_bucket(self) -> int:
        """Raw hour-of-day (0..23). Use Config-aware bucket via time_bucket()."""
        return self.taken_at.hour if self.taken_at else 12


def time_bucket(hour: int, num_buckets: int, day_start: int, day_end: int) -> int:
    """Map an hour-of-day to a coarse day-period bucket in [0, num_buckets).

    Anything outside the [day_start, day_end) window collapses into bucket 0
    (treated as 'pre-dawn / post-dusk' — we expect almost no triggers there).
    """
    if hour < day_start or hour >= day_end:
        return 0
    span = max(day_end - day_start, 1)
    idx = (hour - day_start) * num_buckets // span
    return max(0, min(num_buckets - 1, idx))


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
