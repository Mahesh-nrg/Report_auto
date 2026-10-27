"""CVSS v3.x base score from a vector, so score and severity are calculated, never guessed.

The LLM proposes a *vector* (a judgement about the finding); the arithmetic is done here, following
the FIRST CVSS specification, so the score, the severity word and PwnDoc's own CVSS field always
agree with each other.

Versions: ``CVSS:3.1`` (and a vector with no prefix) use the 3.1 Roundup; ``CVSS:3.0`` keeps its
own label and uses the 3.0 rounding (plain ceiling to one decimal). The base formula itself is
identical in both versions.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

PREFIX = "CVSS:3.1/"
PREFIX_3_0 = "CVSS:3.0/"
ORDER = ("AV", "AC", "PR", "UI", "S", "C", "I", "A")
MAX_VECTOR_LEN = 200
_WEIGHTS = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "C": {"H": 0.56, "L": 0.22, "N": 0.0},
    "I": {"H": 0.56, "L": 0.22, "N": 0.0},
    "A": {"H": 0.56, "L": 0.22, "N": 0.0},
}
_PRIVILEGES = {
    "unchanged": {"N": 0.85, "L": 0.62, "H": 0.27},
    "changed": {"N": 0.85, "L": 0.68, "H": 0.5},
}
_ALLOWED = {**{k: set(v) for k, v in _WEIGHTS.items()}, "PR": {"N", "L", "H"}, "S": {"U", "C"}}
# Temporal / environmental metrics: validated against the spec, then ignored for the base score.
_OPTIONAL = {
    "E": set("XUPFH"),
    "RL": set("XOTWU"),
    "RC": set("XURC"),
    **{k: set("XLMH") for k in ("CR", "IR", "AR")},
    "MAV": set("XNALP"),
    "MAC": set("XLH"),
    "MPR": set("XNLH"),
    "MUI": set("XNR"),
    "MS": set("XUC"),
    **{k: set("XNLH") for k in ("MC", "MI", "MA")},
}


# Labels of the custom fields that hold the base score / the severity rating. Anchored on purpose:
# "CVSS Temporal Score", "Severity justification" or a bare "CVSS" (often the vector string) are
# different fields and must never be overwritten with a computed value.
_SEP = r"[\s_-]*"
_SCORE_LABEL = re.compile(
    rf"^(?:cvss(?:{_SEP}v?3(?:\.[01])?)?{_SEP}(?:base{_SEP})?|base{_SEP})score$", re.IGNORECASE
)
_SEVERITY_LABEL = re.compile(
    rf"^(?:cvss(?:{_SEP}v?3(?:\.[01])?)?{_SEP})?(?:severity|risk{_SEP}rating)$", re.IGNORECASE
)


def is_score_label(label: Any) -> bool:
    """True for a field that holds the CVSS base score (not temporal/environmental/vector)."""
    return isinstance(label, str) and bool(_SCORE_LABEL.match(label.strip()))


def is_severity_label(label: Any) -> bool:
    """True for a field that holds the severity rating (not 'severity justification')."""
    return isinstance(label, str) and bool(_SEVERITY_LABEL.match(label.strip()))


@dataclass(frozen=True)
class Cvss:
    vector: str
    score: float
    severity: str


def severity_of(score: float) -> str:
    if score <= 0:
        return "None"
    if score < 4.0:
        return "Low"
    if score < 7.0:
        return "Medium"
    if score < 9.0:
        return "High"
    return "Critical"


def _roundup(value: float) -> float:
    """The specification's Roundup: smallest one-decimal number >= value, float-error safe."""
    scaled = round(value * 100000)
    if scaled % 10000 == 0:
        return scaled / 100000.0
    return (math.floor(scaled / 10000) + 1) / 10.0


def _roundup_3_0(value: float) -> float:
    """CVSS 3.0 rounding: the plain ceiling to one decimal."""
    return math.ceil(value * 10) / 10


def _base_score(m: dict[str, str], roundup: Callable[[float], float] = _roundup) -> float:
    changed = m["S"] == "C"
    iss = 1 - (1 - _WEIGHTS["C"][m["C"]]) * (1 - _WEIGHTS["I"][m["I"]]) * (
        1 - _WEIGHTS["A"][m["A"]]
    )
    impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if changed else 6.42 * iss
    privileges = _PRIVILEGES["changed" if changed else "unchanged"][m["PR"]]
    exploitability = (
        8.22
        * _WEIGHTS["AV"][m["AV"]]
        * _WEIGHTS["AC"][m["AC"]]
        * privileges
        * _WEIGHTS["UI"][m["UI"]]
    )
    if impact <= 0:
        return 0.0
    total = 1.08 * (impact + exploitability) if changed else impact + exploitability
    return roundup(min(total, 10))


def parse_vector(vector: Any) -> Cvss | None:
    """Parse and score a CVSS v3.x base vector; ``None`` if it is not a complete, valid one."""
    if not isinstance(vector, str) or not vector.strip() or len(vector) > MAX_VECTOR_LEN:
        return None
    parts = vector.strip().upper().split("/")
    prefix, roundup = PREFIX, _roundup
    if parts[0].startswith("CVSS:"):
        if parts[0] not in ("CVSS:3.1", "CVSS:3.0"):
            return None  # v4 and others have different metrics
        if parts[0] == "CVSS:3.0":
            prefix, roundup = PREFIX_3_0, _roundup_3_0
        parts = parts[1:]
    metrics: dict[str, str] = {}
    for part in parts:
        name, _, value = part.partition(":")
        allowed = _ALLOWED.get(name) or _OPTIONAL.get(name)
        if name in metrics or allowed is None or value not in allowed:
            return None  # duplicate, unknown metric, empty segment or invalid value
        metrics[name] = value
    if not set(ORDER) <= set(metrics):
        return None
    canonical = prefix + "/".join(f"{name}:{metrics[name]}" for name in ORDER)
    score = _base_score(metrics, roundup)
    return Cvss(canonical, score, severity_of(score))
