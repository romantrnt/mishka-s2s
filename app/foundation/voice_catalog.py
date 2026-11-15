"""Stable IDs for the recorded Russian phrase catalog."""
from __future__ import annotations

import re
from pathlib import Path


REQUIRED_PHRASE_IDS = frozenset(
    f"PHRASE_{number:03d}" for number in range(1, 109)
    if number not in {72, 86, 88, 89, 90, 92}
)
REQUIRED_COUNT = len(REQUIRED_PHRASE_IDS)
_PHRASE_ID = re.compile(r"PHRASE_[0-9]{3}\Z")


def load_phrase_catalog(path: str | Path) -> dict[str, str]:
    """Read explicit ID<TAB>transcript rows; never infer an ID from line order."""
    phrases: dict[str, str] = {}
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 2:
            raise ValueError(f"catalog line {number} must be ID<TAB>transcript")
        phrase_id, transcript = fields
        if not _PHRASE_ID.fullmatch(phrase_id):
            raise ValueError(f"catalog line {number} has invalid phrase ID")
        if not transcript or transcript != transcript.strip():
            raise ValueError(f"catalog line {number} has an empty or untrimmed transcript")
        if phrase_id in phrases:
            raise ValueError(f"catalog line {number} repeats {phrase_id}")
        phrases[phrase_id] = transcript
    return phrases
