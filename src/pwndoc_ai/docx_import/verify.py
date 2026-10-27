"""The order/severity verification report of an import plan and of the audit it created.

One entry per finding with every check (``pass``/``fail``) plus the overall list checks
(order monotonic by score, numbering contiguous, duplicate titles). Pure functions; the created
audit is read as the PwnDoc finding dicts (``cvssv3`` and the ``cvssscore``/``severity`` custom
fields).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from .. import cvss
from . import ordering
from .model import ImportedFinding, ImportPlan
from .normalize import ESTIMATED_FLAG, normalize_findings

PASS, FAIL, FIXED = "pass", "fail", "fixed"


def _check(ok: bool) -> str:
    return PASS if ok else FAIL


def _entry(
    seq: str, finding: Mapping[str, Any], stored_severity: str, *, fixes: bool = False
) -> dict[str, Any]:
    vector = finding.get("cvss_vector") or ""
    parsed = cvss.parse_vector(vector)
    score = ordering.score_from(vector, finding.get("cvss_score"))
    band = ordering.band_of_score(score) if score is not None else ""
    label = ordering.band_of_label(stored_severity)
    printed = ordering.numeric_score(finding.get("cvss_score"))
    checks = {
        "cvss_present": _check(score is not None),
        "cvss_vector_valid": _check(parsed is not None),
        "score_matches_vector": _check(parsed is None or printed in (None, parsed.score)),
        "severity_matches_band": _band_check(band, label, fixes),
    }
    note = f"{stored_severity or '-'} -> {band}" if band and label != band else ""
    return {
        "seq": seq,
        "title": finding.get("title") or "",
        "score": score,
        "severity": band or (label if label != ordering.UNKNOWN else ""),
        "stored_severity": stored_severity,
        "checks": checks,
        "note": note,
    }


def _band_check(band: str, label: str, fixes: bool) -> str:
    if not band or label == band:
        return PASS
    return FIXED if fixes else FAIL


def _list_checks(entries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    keys = [
        (ordering.BANDS.index(e["severity"] or ordering.UNKNOWN), -(e["score"] or 0.0))
        for e in entries
    ]
    width = max(3, len(str(len(entries))))
    seqs = [str(n).zfill(width) for n in range(1, len(entries) + 1)]
    titles = Counter(" ".join(str(e["title"]).lower().split()) for e in entries)
    return {
        "order_monotonic": _check(keys == sorted(keys)),
        "numbering_contiguous": _check([e["seq"] for e in entries] == seqs),
        "duplicate_titles": sorted(t for t, n in titles.items() if n > 1 and t),
    }


def _report(entries: list[dict[str, Any]]) -> dict[str, Any]:
    checks = _list_checks(entries)
    failed = sum(v == FAIL for e in entries for v in e["checks"].values())
    failed += sum(checks[k] == FAIL for k in ("order_monotonic", "numbering_contiguous"))
    failed += len(checks["duplicate_titles"])
    return {"ok": failed == 0, "issues": failed, "checks": checks, "findings": entries}


def verify_plan(plan: ImportPlan) -> dict[str, Any]:
    """The report of the preview: the findings as the writer will create them (Part B applied,
    creation order) with their new S. No.; ``estimated`` marks a calculated CVSS vector."""
    stored = {f.key: f.severity for f in plan.findings}
    ordered = [f for f in normalize_findings(plan.findings) if f.include]
    width = max(3, len(str(len(ordered))))
    entries = []
    for n, finding in enumerate(ordered, start=1):
        entry = _entry(str(n).zfill(width), _abstract(finding), stored[finding.key], fixes=True)
        estimated = ESTIMATED_FLAG in finding.flags
        entries.append({**entry, "key": finding.key, "estimated": estimated})
    return _report(entries)


def _abstract(finding: ImportedFinding) -> dict[str, Any]:
    return ordering.abstract(finding)


def _custom(finding: Mapping[str, Any], name: str) -> str:
    for field in finding.get("customFields") or ():
        if not isinstance(field, Mapping):
            continue
        definition = field.get("customField")
        label = definition.get("label", "") if isinstance(definition, Mapping) else ""
        if "".join(ch for ch in str(label).lower() if ch.isalnum()) == name:
            text = field.get("text")
            return text if isinstance(text, str) else ""
    return ""


def verify_audit(findings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The report of the created audit, read in PwnDoc's stored order."""
    width = max(3, len(str(len(findings))))
    entries = []
    for number, raw in enumerate(findings, start=1):
        abstract = {
            "title": raw.get("title") or "",
            "cvss_vector": raw.get("cvssv3") or "",
            "cvss_score": _custom(raw, "cvssscore"),
        }
        entries.append(_entry(str(number).zfill(width), abstract, _custom(raw, "severity")))
    return _report(entries)


__all__ = ["verify_audit", "verify_plan"]
