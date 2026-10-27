"""The gateway's ordered, normalised view of an audit's findings (docs/FINDING_ORDER.md).

``build_plan`` runs the deterministic normaliser (``finding_normalize``, imported lazily) on
every finding, sorts them by the Part A key, numbers them 001.., and maps each row back to the
PwnDoc PUT payload that would store it (``finding_adapter.write_back``). ``verification`` turns a
plan into the B8 report. Nothing is written here: see ``finding_apply`` for that.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .cvss import parse_vector
from .finding_adapter import (
    NA,
    absent_fields,
    is_estimated_source,
    plain,
    to_abstract,
    write_back,
)

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
TEXT_FIELDS = (
    "title", "location", "path", "parameter", "description", "observation",
    "poc", "steps", "remediation", "references", "affected",
)  # fmt: skip
CWE_FORMAT = re.compile(r"^(N/A|CWE-\d+(, CWE-\d+)*)$")
CVE_FORMAT = re.compile(r"^(N/A|CVE-\d{4}-\d{4,7}(, CVE-\d{4}-\d{4,7})*)$")
NO_IMPACT = ("C:N", "I:N", "A:N")
ESTIMATED_NOTE = "estimated by AI/heuristic - review"

Normalizer = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]


class NormalizeUnavailable(RuntimeError):
    """The normalisation module is not installed in this gateway build."""


def default_normalizer() -> Normalizer:
    """``finding_normalize.normalize_finding`` (deterministic path), looked up when needed."""
    try:
        from .finding_normalize import normalize_finding
    except ImportError as exc:
        raise NormalizeUnavailable("finding normalisation is not installed") from exc

    def normalise(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [normalize_finding(item)[0] for item in items]

    return normalise


def band_of_label(label: Any) -> str:
    return LABELS.get(label.strip().lower(), "Unknown") if isinstance(label, str) else "Unknown"


@dataclass(frozen=True)
class PlanRow:
    finding_id: str
    position: int
    identifier: Any
    seq: str
    abstract: Mapping[str, Any]
    normalized: Mapping[str, Any]
    payload: Mapping[str, Any]
    changes: Mapping[str, Mapping[str, str]]
    absent: tuple[str, ...]

    def view(self) -> dict[str, Any]:
        n = self.normalized
        return {
            "id": self.finding_id,
            "seq": self.seq,
            "position": self.position,
            "title": n.get("title"),
            "severity": n.get("severity"),
            "severity_stored": plain(self.abstract.get("severity")),
            "mismatch": bool(n.get("mismatch")),
            "score": n.get("cvss_score"),
            "vector": n.get("cvss_vector"),
            "cvss_source": n.get("cvss_source"),
            "estimated": bool(n.get("cvss_estimated")),
            "cwe": n.get("cwe"),
            "cve": n.get("cve"),
            "changes": {k: dict(v) for k, v in self.changes.items()},
            "field_absent": list(self.absent),
        }


def _finding_id(finding: Mapping[str, Any]) -> str:
    return str(finding.get("_id") or finding.get("id") or "")


def identifier_index(identifier: Any, position: int) -> int:
    """Tie-break of Part A, as the template filter's identifierIndex: the stored S. No."""
    if isinstance(identifier, int) and not isinstance(identifier, bool):
        return identifier
    digits = "".join(c for c in identifier if c.isdigit()) if isinstance(identifier, str) else ""
    return int(digits) if digits else position


def _sort_key(position: int, n: Mapping[str, Any], identifier: Any) -> tuple[Any, ...]:
    score = n.get("cvss_score")
    band = n.get("severity") if n.get("severity") in BANDS else "Unknown"
    tie = identifier_index(identifier, position)
    return (BANDS.index(band), score is None, -(score or 0.0), tie, position)


def _normalise_one(normalizer: Normalizer, abstract: dict[str, Any]) -> dict[str, Any]:
    rows = normalizer([abstract])
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise NormalizeUnavailable("the normaliser returned an unexpected shape")
    return _keep_estimate_marker(abstract, dict(rows[0]))


def _keep_estimate_marker(abstract: Mapping[str, Any], n: dict[str, Any]) -> dict[str, Any]:
    """A vector written earlier as an estimate stays flagged until the user changes it."""
    stored = abstract.get("cvss_source_stored")
    vector = n.get("cvss_vector")
    same_vector = vector == abstract.get("cvss_vector") and str(stored).endswith(str(vector))
    if n.get("cvss_source") == "vector" and same_vector and is_estimated_source(stored):
        return {**n, "cvss_source": stored, "cvss_estimated": True}
    return n


def build_plan(
    findings: list[Mapping[str, Any]], normalizer: Normalizer | None = None
) -> list[PlanRow]:
    """Every finding normalised, in report order, with the payload that would store it."""
    norm = normalizer or default_normalizer()
    usable = [f for f in findings if isinstance(f, Mapping) and _finding_id(f)]
    abstracts = [to_abstract(f, i) for i, f in enumerate(usable)]
    normalized = [_normalise_one(norm, a) for a in abstracts]
    ordered = sorted(
        enumerate(normalized),
        key=lambda row: _sort_key(row[0], row[1], usable[row[0]].get("identifier")),
    )
    width = max(3, len(str(len(ordered))))
    plan = []
    for rank, (position, n) in enumerate(ordered):
        finding = usable[position]
        payload, changes = write_back(finding, n)
        plan.append(
            PlanRow(
                _finding_id(finding),
                position,
                finding.get("identifier"),
                str(rank + 1).zfill(width),
                abstracts[position],
                n,
                payload,
                changes,
                tuple(absent_fields(finding)),
            )
        )
    return plan


def plan_digest(plan: list[PlanRow]) -> str:
    """Identifies exactly what the user reviewed: applying refuses a plan that changed since."""
    body = [[r.finding_id, r.seq, r.payload] for r in plan]
    raw = json.dumps(body, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def needs_write(plan: list[PlanRow]) -> bool:
    return any(r.payload for r in plan) or any(r.position != i for i, r in enumerate(plan))


# -- verification (B8) ---------------------------------------------------------------------
def _check(cid: str, row: PlanRow | None, status: str, detail: str = "") -> dict[str, Any]:
    return {
        "id": cid,
        "finding_seq": row.seq if row else None,
        "title": plain(row.normalized.get("title")) if row else None,
        "status": status,
        "detail": detail,
    }


def _filled(row: PlanRow) -> list[str]:
    """Fields normalisation turns into N/A (roles the audit type does not define are skipped)."""
    return [
        k
        for k in TEXT_FIELDS
        if k not in row.absent and row.normalized.get(k) == NA and plain(row.abstract.get(k)) != NA
    ]


def _score_check(row: PlanRow) -> str:
    if "score" in row.absent:
        return "pass"
    stored = plain(row.abstract.get("cvss_score"))
    return "pass" if stored and stored == f"{row.normalized.get('cvss_score'):.1f}" else "fixed"


def _vector_check(row: PlanRow) -> str:
    vector = row.normalized.get("cvss_vector")
    if parse_vector(vector) is not None:
        return "pass"
    return "warn" if vector == NA and row.normalized.get("cvss_source") == "numeric" else "fail"


def _info_check(row: PlanRow) -> str:
    n = row.normalized
    if n.get("cvss_score") != 0.0:
        return "pass"
    vector = str(n.get("cvss_vector") or "")
    ok = all(m in vector.split("/") for m in NO_IMPACT) and n.get("cwe") == n.get("cve") == NA
    return "pass" if ok else "fail"


def _finding_checks(row: PlanRow) -> list[dict[str, Any]]:
    n = row.normalized
    filled = _filled(row)
    label = band_of_label(row.abstract.get("severity"))
    if "severity" in row.absent:
        label = str(n.get("severity"))
    checks = [
        _check("V01", row, "fixed" if filled else "pass", ", ".join(filled)),
        _check("V02", row, _vector_check(row)),
        _check("V03", row, _score_check(row)),
        _check("V04", row, "pass" if label == n.get("severity") else "fixed", label),
        _check("V05", row, "pass" if CWE_FORMAT.match(str(n.get("cwe"))) else "fail"),
        _check("V06", row, "pass" if CVE_FORMAT.match(str(n.get("cve"))) else "fail"),
        _check("V07", row, _info_check(row)),
    ]
    if n.get("cvss_estimated"):
        checks.append(_check("V11", row, "warn", ESTIMATED_NOTE))
    if n.get("cvss_v4_vector") not in (None, NA):
        checks.append(_check("V12", row, "warn", "v4 vector ignored; v3.1 was calculated"))
    return checks


def _list_checks(plan: list[PlanRow]) -> list[dict[str, Any]]:
    moved = any(r.position != i for i, r in enumerate(plan))
    numbered = all(str(r.identifier) == str(int(r.seq)) for r in plan)
    titles = [plain(r.normalized.get("title")).lower() for r in plan]
    dupes = sorted({t for t in titles if titles.count(t) > 1 and t != "n/a"})
    return [
        _check("V08", None, "fixed" if moved else "pass", "stored order differs" if moved else ""),
        _check(
            "V09",
            None,
            "pass" if numbered else "warn",
            "" if numbered else "stored identifiers differ; outputs number by seq",
        ),
        _check("V10", None, "warn" if dupes else "pass", ", ".join(dupes)),
    ]


def verification(audit_id: str, plan: list[PlanRow]) -> dict[str, Any]:
    """The B8 report. ``normalised`` is true when nothing needs fixing (warnings allowed)."""
    checks = [c for row in plan for c in _finding_checks(row)] + _list_checks(plan)
    summary = {
        s: sum(1 for c in checks if c["status"] == s) for s in ("pass", "fail", "fixed", "warn")
    }
    return {
        "audit_id": audit_id,
        "normalised": summary["fail"] == 0 and summary["fixed"] == 0,
        "checks": checks,
        "summary": summary,
    }


def header_value(report: Mapping[str, Any]) -> str:
    """Short ``X-Findings-Verified`` value: ``ok`` or ``issues=<fail+fixed>``."""
    summary = report.get("summary") or {}
    issues = int(summary.get("fail", 0)) + int(summary.get("fixed", 0))
    return "ok" if issues == 0 else f"issues={issues}"
