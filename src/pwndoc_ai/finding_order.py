"""Finding order and severity banding (docs/FINDING_ORDER.md part A).

Pure functions: the v3.1 score of a finding (vector first, else a usable number, never a v4
value), its band, the total sort key and the 001.. numbering. Scoring is done by
``pwndoc_ai.cvss`` only. Inputs are never changed; every function returns new objects.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .cvss import MAX_VECTOR_LEN, parse_vector

BANDS = ("Critical", "High", "Medium", "Low", "Informational", "Unknown")
LABEL_ALIASES = {
    "critical": "Critical",
    "high": "High",
    "medium": "Medium",
    "moderate": "Medium",
    "low": "Low",
    "info": "Informational",
    "informational": "Informational",
    "none": "Informational",
}
_NUMBER = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
MAX_NUMBER_LEN = 32
_COMMA_DECIMAL = re.compile(r"^[0-9]{1,2},[0-9]$")  # '7,5' only; '1,234' is not a score
_CANONICAL = re.compile(r"^[0-9]{1,2}(?:\.[0-9])?$")
MAX_LABEL_LEN = 64


def as_mapping(item: Any) -> Mapping[str, Any]:
    """A non-dict item (hostile input) is read as an empty finding."""
    return item if isinstance(item, Mapping) else {}


def roundup_31(value: float) -> float:
    """CVSS v3.1 Appendix A Roundup (integer method)."""
    scaled = round(value * 100000)
    if scaled % 10000 == 0:
        return scaled / 100000.0
    return (math.floor(scaled / 10000) + 1) / 10.0


def vector_version(vector: Any) -> str | None:
    """'3.1', '3.0', '4' (any CVSS:4.x), 'other', or None (no prefix / not a string)."""
    if not isinstance(vector, str):
        return None
    head = vector[: MAX_VECTOR_LEN + 1].strip().upper().split("/")[0]
    if not head.startswith("CVSS:"):
        return None
    known = {"CVSS:3.1": "3.1", "CVSS:3.0": "3.0"}
    return known.get(head, "4" if head.startswith("CVSS:4") else "other")


def vector_score(vector: Any) -> float | None:
    parsed = parse_vector(vector)
    return None if parsed is None else parsed.score


def numeric_score(raw: Any) -> float | None:
    """Part A step 3: a usable 0..10 number, rounded with 3.1 Roundup, else None."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str) and len(raw) <= MAX_NUMBER_LEN:
        text = raw.strip()
        if _COMMA_DECIMAL.match(text):
            text = text.replace(",", ".")
        if not _NUMBER.match(text):
            return None
        value = float(text)
    else:
        return None
    if math.isnan(value) or value < 0 or value > 10:
        return None
    return roundup_31(value)


def is_canonical_score(raw: Any) -> bool:
    """False when a usable score needed rounding (e.g. '3.95'), so verification can warn."""
    if numeric_score(raw) is None:
        return True
    text = repr(float(raw)) if isinstance(raw, (int, float)) else raw.strip().replace(",", ".")
    return bool(_CANONICAL.match(text.removesuffix(".0") if text.endswith(".0") else text))


def is_other_version(vector: Any) -> bool:
    return vector_version(vector) in ("4", "other")


def score_of(finding: Any) -> float | None:
    f = as_mapping(finding)
    vector = f.get("cvss_vector")
    from_vector = vector_score(vector)
    if from_vector is not None:
        return from_vector
    if is_other_version(vector):
        return None  # the number belongs to that other version
    return numeric_score(f.get("cvss_score"))


def band_of_score(score: float) -> str:
    if score == 0:
        return "Informational"
    if score < 4.0:
        return "Low"
    if score < 7.0:
        return "Medium"
    if score < 9.0:
        return "High"
    return "Critical"


def band_of_label(label: Any) -> str:
    if not isinstance(label, str) or len(label) > MAX_LABEL_LEN:
        return "Unknown"
    return LABEL_ALIASES.get(label.strip().lower(), "Unknown")


def is_mismatch(score: float | None, label: Any) -> bool:
    label_band = band_of_label(label)
    if score is None or label_band == "Unknown":
        return False
    return label_band != band_of_score(score)


def sort_key(score: float | None, band: str, index: Any, position: int) -> tuple:
    rank = BANDS.index(band) if band in BANDS else BANDS.index("Unknown")
    usable_index = index if isinstance(index, int) and not isinstance(index, bool) else position
    return (rank, score is None, -(score or 0.0), usable_index, position)


def seq_width(count: int) -> int:
    return max(3, len(str(count)))


def number(position: int, count: int) -> str:
    return str(position + 1).zfill(seq_width(count))


def _order_row(position: int, item: Any) -> tuple[tuple, dict[str, Any]]:
    f = as_mapping(item)
    score = score_of(f)
    band = band_of_score(score) if score is not None else band_of_label(f.get("severity"))
    extra = {
        "order_score": score,
        "order_band": band,
        "mismatch": is_mismatch(score, f.get("severity")),
    }
    key = sort_key(score, band, f.get("index"), position)
    return key, {**f, **extra}


def order_findings(findings: Sequence[Any]) -> list[dict[str, Any]]:
    """New finding dicts in report order, each with ``order_score``, ``order_band``,
    ``mismatch`` and ``seq`` added. The input list and its items are not changed."""
    rows = sorted((_order_row(i, f) for i, f in enumerate(findings or ())), key=lambda r: r[0])
    count = len(rows)
    return [{**row, "seq": number(i, count)} for i, (_, row) in enumerate(rows)]


def order_summary(findings: Sequence[Any]) -> list[dict[str, Any]]:
    """The spec's comparison shape: ``{title, score, band, seq, mismatch}`` per finding."""
    return [
        {
            "title": row.get("title"),
            "score": row["order_score"],
            "band": row["order_band"],
            "seq": row["seq"],
            "mismatch": row["mismatch"],
        }
        for row in order_findings(findings)
    ]
