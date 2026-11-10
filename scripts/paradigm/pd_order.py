"""Finding order of the generated Word deliverables (docs/FINDING_ORDER.md part A).

The example findings of the Word report and the import sample are listed Critical -> High ->
Medium -> Low -> Informational by CVSS v3.1 base score (highest first, ties keep the original
S. No.), then renumbered 001.. so the register, the detail tables and the 'Finding 001 - ...'
headings agree. The severity shown is the band DERIVED from the score; the label only decides
when there is no usable score. Same contract as the PwnDoc ``sortFindings`` filter.
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pwndoc_ai.cvss import parse_vector  # noqa: E402

BANDS = ("Critical", "High", "Medium", "Low", "Informational", "Unknown")
LABELS = {
    "critical": "Critical",
    "high": "High",
    "medium": "Medium",
    "moderate": "Medium",
    "low": "Low",
    "info": "Informational",
    "informational": "Informational",
    "none": "Informational",
}
_VECTOR = re.compile(r"(?:CVSS:[0-9.]+/)?AV:[A-Z]/\S+", re.IGNORECASE)
_NUMBER = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


def _roundup(value: float) -> float:
    scaled = round(value * 100000)
    if scaled % 10000 == 0:
        return scaled / 100000.0
    return (math.floor(scaled / 10000) + 1) / 10.0


def numeric_score(raw: Any) -> float | None:
    """Part A step 3: a 0-10 number (``7,5`` accepted), rounded up to one decimal."""
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
    if math.isnan(value) or not 0 <= value <= 10:
        return None
    return _roundup(value)


def content_score(cvss_text: Any) -> float | None:
    """Score of a content ``cvss`` cell: an embedded v3.x vector wins, else the leading number."""
    if not isinstance(cvss_text, str):
        return numeric_score(cvss_text)
    match = _VECTOR.search(cvss_text)
    if match:
        if match.group(0).upper().startswith("CVSS:4"):
            return None
        parsed = parse_vector(match.group(0))
        if parsed is not None:
            return parsed.score
    return numeric_score(cvss_text.split("(")[0])


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
    return LABELS.get(label.strip().lower(), "Unknown") if isinstance(label, str) else "Unknown"


def _key(item: dict, position: int) -> tuple:
    score = content_score(item.get("cvss"))
    band = band_of_score(score) if score is not None else band_of_label(item.get("severity"))
    index = int(item["id"]) if str(item.get("id", "")).isdigit() else position
    return (BANDS.index(band), score is None, -(score or 0.0), index, position)


def _display_severity(item: dict) -> str:
    score = content_score(item.get("cvss"))
    if score is None:
        return item.get("severity", "")
    band = band_of_score(score)
    return "Info" if band == "Informational" and item.get("severity") == "Info" else band


def order_content(content: dict) -> dict:
    """New content dict: findings sorted + renumbered, register rebuilt in the same order.

    ``src_id`` keeps the original id (evidence image names); the input is never changed.
    """
    findings = content.get("findings", [])
    ranked = sorted(range(len(findings)), key=lambda i: _key(findings[i], i))
    width = max(3, len(str(len(findings))))
    register = {row.get("id"): row for row in content.get("register", [])}
    new_findings, new_register = [], []
    for seq, position in enumerate(ranked, 1):
        item = findings[position]
        sno = str(seq).zfill(width)
        severity = _display_severity(item)
        src = item.get("src_id", item.get("id"))
        new_findings.append({**item, "id": sno, "src_id": src, "severity": severity})
        if item.get("id") in register:
            new_register.append({**register[item["id"]], "id": sno, "sev": severity})
    return {**content, "findings": new_findings, "register": new_register}
