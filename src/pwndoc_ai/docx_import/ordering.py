"""Finding order, derived severity and numbering for the importer (docs/FINDING_ORDER.md Part A).

The CVSS v3.1 base score decides everything: the score comes from a valid ``CVSS:3.1``/``3.0``
vector (computed by ``pwndoc_ai.cvss``), else from the printed number; a ``CVSS:4`` (or other)
vector blocks the number. The band of the score is the severity; only a finding without a score
keeps its label, and sorts after the scored findings of that band. Ties keep the plan order.

``order_rows`` is the pure Part A function on abstract dicts (tested against
``tests/data/finding_order_cases.json``); the other helpers adapt ``ImportedFinding``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .. import cvss
from .model import ImportedFinding

BANDS = ("Critical", "High", "Medium", "Low", "Informational", "Unknown")
UNKNOWN = "Unknown"
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


def _vector_head(vector: Any) -> str | None:
    if not isinstance(vector, str):
        return None
    head = vector.strip().upper().split("/")[0]
    return head if head.startswith("CVSS:") else None


def numeric_score(raw: Any) -> float | None:
    """A printed score as a v3.1 number (``"7,5"`` -> 7.5), or ``None`` when unusable."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if text.count(",") == 1 and "." not in text:
            text = text.replace(",", ".")
        if not _NUMBER.match(text):
            return None
        value = float(text)
    else:
        return None
    if math.isnan(value) or value < 0 or value > 10:
        return None
    return cvss._roundup(value)


def score_from(vector: Any, score: Any) -> float | None:
    """Part A section 1: vector first, a non-v3 vector blocks the number, then the number."""
    parsed = cvss.parse_vector(vector)
    if parsed is not None:
        return parsed.score
    head = _vector_head(vector)
    if head is not None and head not in ("CVSS:3.1", "CVSS:3.0"):
        return None
    return numeric_score(score)


def band_of_score(score: float) -> str:
    if score == 0:
        return "Informational"
    severity = cvss.severity_of(score)
    return severity if severity in BANDS else "Informational"


def band_of_label(label: Any) -> str:
    if not isinstance(label, str):
        return UNKNOWN
    return LABEL_ALIASES.get(label.strip().lower(), UNKNOWN)


def _row(finding: Mapping[str, Any], position: int) -> tuple[tuple, dict[str, Any]]:
    score = score_from(finding.get("cvss_vector"), finding.get("cvss_score"))
    label_band = band_of_label(finding.get("severity"))
    band = band_of_score(score) if score is not None else label_band
    mismatch = score is not None and label_band != UNKNOWN and label_band != band
    index = finding.get("index", position)
    key = (BANDS.index(band), score is None, -(score or 0.0), index, position)
    return key, {"title": finding.get("title"), "score": score, "band": band, "mismatch": mismatch}


def seq_text(number: int, count: int) -> str:
    return str(number).zfill(max(3, len(str(count))))


def order_rows(findings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Part A on abstract findings: rows ``{title, score, band, seq, mismatch, position}``."""
    rows = sorted(
        ((*_row(finding, position), position) for position, finding in enumerate(findings)),
        key=lambda row: row[0],
    )
    return [
        {**data, "seq": seq_text(number, len(rows)), "position": position}
        for number, (_, data, position) in enumerate(rows, start=1)
    ]


# -- ImportedFinding adapters --------------------------------------------------------------------


def abstract(finding: ImportedFinding) -> dict[str, Any]:
    return {
        "title": finding.title,
        "cvss_vector": finding.cvss_vector,
        "cvss_score": finding.cvss_score,
        "severity": finding.severity,
    }


def finding_score(finding: ImportedFinding) -> float | None:
    return score_from(finding.cvss_vector, finding.cvss_score)


def derived_severity(finding: ImportedFinding) -> str:
    """The band of the v3.1 score; without a score the stored label (``""`` when unknown)."""
    score = finding_score(finding)
    if score is not None:
        return band_of_score(score)
    band = band_of_label(finding.severity)
    return "" if band == UNKNOWN else band


def is_mismatch(finding: ImportedFinding) -> bool:
    score = finding_score(finding)
    label = band_of_label(finding.severity)
    return score is not None and label != UNKNOWN and label != band_of_score(score)


def sort_findings(findings: Sequence[ImportedFinding]) -> tuple[ImportedFinding, ...]:
    """Every finding (excluded ones too) in Part A order; stable, so sorting twice is a no-op."""
    rows = order_rows([abstract(f) for f in findings])
    return tuple(findings[row["position"]] for row in rows)


def numbers(findings: Sequence[ImportedFinding]) -> dict[str, str]:
    """``key -> "001"`` for the included findings, in Part A order."""
    included = [f for f in findings if f.include]
    rows = order_rows([abstract(f) for f in included])
    return {included[row["position"]].key: row["seq"] for row in rows}


__all__ = [
    "BANDS",
    "abstract",
    "band_of_label",
    "band_of_score",
    "derived_severity",
    "finding_score",
    "is_mismatch",
    "numbers",
    "numeric_score",
    "order_rows",
    "score_from",
    "seq_text",
    "sort_findings",
]
