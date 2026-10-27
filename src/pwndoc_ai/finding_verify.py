"""Verification report of a findings list (docs/FINDING_ORDER.md B8), JSON-serialisable.

``verify(rows, originals=None)`` checks rows of the abstract output model (normally the result
of ``normalize_audit``; raw rows simply fail where they are not normalised). With ``originals``
(the input list, matched through each row's ``position``) a corrected value is reported as
``fixed`` instead of ``pass``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .cvss import parse_vector
from .finding_fields import (
    CVE_LIST,
    CWE_LIST,
    NA,
    TEXT_FIELDS,
    cve_in,
    finding_text,
    is_placeholder,
)
from .finding_order import (
    BANDS,
    as_mapping,
    band_of_label,
    band_of_score,
    is_canonical_score,
    number,
    numeric_score,
    score_of,
    vector_version,
)

REPORTED_FIELDS = (*TEXT_FIELDS, "severity", "cvss_vector", "cwe", "cve")
STATUSES = ("pass", "fail", "fixed", "warn")
ESTIMATED_NOTE = "estimated by AI/heuristic - review"


def _check(cid: str, row: Mapping | None, status: str, detail: str = "") -> dict[str, Any]:
    seq = row.get("seq") if row is not None else None
    title = row.get("title") if row is not None else None
    return {
        "id": cid,
        "finding_seq": seq if isinstance(seq, str) else None,
        "title": title if isinstance(title, str) else None,
        "status": status,
        "detail": detail,
    }


def _original(row: Mapping, originals: Sequence[Any] | None) -> Mapping | None:
    pos = row.get("position")
    if originals is None or not isinstance(pos, int) or isinstance(pos, bool):
        return None
    return as_mapping(originals[pos]) if 0 <= pos < len(originals) else None


def _fields_present(row: Mapping, orig: Mapping | None) -> dict:
    missing = [k for k in REPORTED_FIELDS if row.get(k) is None or row.get(k) == ""]
    missing += [k for k in ("cvss_score",) if not isinstance(row.get(k), (int, float))]
    if missing:
        return _check("V01_fields_present", row, "fail", "missing: " + ", ".join(missing))
    ai = [k for k in row.get("ai_filled") or [] if isinstance(k, str)]
    na = [
        k for k in TEXT_FIELDS if orig is not None and is_placeholder(orig.get(k)) and k not in ai
    ]
    parts = [f"ai: {', '.join(ai)}" if ai else "", f"N/A: {', '.join(na)}" if na else ""]
    if ai or na:
        return _check("V01_fields_present", row, "fixed", "; ".join(p for p in parts if p))
    return _check("V01_fields_present", row, "pass")


def _vector_checks(row: Mapping, orig: Mapping | None) -> list[dict]:
    vector, score = row.get("cvss_vector"), row.get("cvss_score")
    parsed = parse_vector(vector)
    if parsed is None and vector == NA and row.get("cvss_source") == "numeric":
        v02 = _check("V02_vector_valid", row, "warn", "score only, vector not recoverable")
        v03 = _check("V03_score_matches_vector", row, "warn", "no vector to compare")
        return [v02, v03]
    if parsed is None:
        return [
            _check("V02_vector_valid", row, "fail", "vector does not parse"),
            _check("V03_score_matches_vector", row, "fail", "no valid vector"),
        ]
    v02 = _check("V02_vector_valid", row, "pass")
    if score != parsed.score:
        return [
            v02,
            _check(
                "V03_score_matches_vector",
                row,
                "fail",
                f"score {score!r} != {parsed.score} from the vector",
            ),
        ]
    before = score_of(orig) if orig is not None else score
    if before is None and orig is not None:
        before = numeric_score(orig.get("cvss_score"))
    status = "pass" if before == parsed.score else "fixed"
    return [v02, _check("V03_score_matches_vector", row, status)]


def _severity_check(row: Mapping, orig: Mapping | None) -> dict:
    score = row.get("cvss_score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        return _check("V04_severity_matches_band", row, "fail", "no score")
    band = band_of_score(score)
    if row.get("severity") != band:
        return _check(
            "V04_severity_matches_band",
            row,
            "fail",
            f"severity_table_mismatch: {row.get('severity')!r} != {band}",
        )
    label = band_of_label(orig.get("severity")) if orig is not None else band
    if label not in ("Unknown", band):
        return _check(
            "V04_severity_matches_band",
            row,
            "fixed",
            f"severity_table_mismatch: label {label} -> {band}",
        )
    return _check("V04_severity_matches_band", row, "pass")


def _id_checks(row: Mapping, orig: Mapping | None) -> list[dict]:
    cwe, cve = row.get("cwe"), row.get("cve")
    ok_cwe = cwe == NA or (isinstance(cwe, str) and bool(CWE_LIST.match(cwe)))
    ok_cve = cve == NA or (isinstance(cve, str) and bool(CVE_LIST.match(cve)))
    v05 = _check("V05_cwe_format", row, "pass" if ok_cwe else "fail")
    detail = "" if ok_cve else "bad format"
    if ok_cve and cve != NA and orig is not None:
        sourced = set((row.get("ai_sources") or {}).get("cve") or [])
        absent = [c for c in cve.split(", ") if c not in cve_in(finding_text(orig))]
        if absent and not sourced:
            ok_cve, detail = False, "not in the source text: " + ", ".join(absent)
    return [v05, _check("V06_cve_format", row, "pass" if ok_cve else "fail", detail)]


def _informational_check(row: Mapping) -> dict:
    if row.get("cvss_score") != 0:
        return _check("V07_informational_rule", row, "pass")
    parsed = parse_vector(row.get("cvss_vector"))
    zero_impact = parsed is not None and parsed.score == 0
    ok = zero_impact and row.get("cwe") == NA and row.get("cve") == NA
    return _check(
        "V07_informational_rule",
        row,
        "pass" if ok else "fail",
        "" if ok else "score 0.0 needs a no-impact vector and CWE/CVE N/A",
    )


def _row_checks(row: Mapping, orig: Mapping | None) -> list[dict]:
    checks = [_fields_present(row, orig), *_vector_checks(row, orig), _severity_check(row, orig)]
    checks += [*_id_checks(row, orig), _informational_check(row)]
    if row.get("cvss_estimated") is True:
        checks.append(_check("V11_estimated_review", row, "warn", ESTIMATED_NOTE))
    if orig is not None and not is_canonical_score(orig.get("cvss_score")):
        checks.append(_check("V13_score_not_canonical", row, "warn", "score had >1 decimal"))
    raw_v4 = row.get("cvss_v4_vector")
    if vector_version(raw_v4) == "4" or (
        orig is not None and vector_version(orig.get("cvss_vector")) == "4"
    ):
        checks.append(_check("V12_cvss_v4_ignored", row, "warn", "v3.1 was calculated"))
    return checks


def _row_key(row: Mapping, position: int) -> tuple:
    score = row.get("cvss_score")
    usable = isinstance(score, (int, float)) and not isinstance(score, bool)
    band = row.get("severity") if row.get("severity") in BANDS else "Unknown"
    index = row.get("index")
    index = index if isinstance(index, int) and not isinstance(index, bool) else position
    pos = row.get("position")
    pos = pos if isinstance(pos, int) and not isinstance(pos, bool) else position
    return (BANDS.index(band), not usable, -(score if usable else 0.0), index, pos)


def _list_checks(rows: list[Mapping]) -> list[dict]:
    keys = [_row_key(r, i) for i, r in enumerate(rows)]
    bad = [i for i in range(1, len(keys)) if keys[i] < keys[i - 1]]
    order = _check(
        "V08_order_monotonic",
        None,
        "fail" if bad else "pass",
        f"out of order at positions {[i + 1 for i in bad[:20]]}" if bad else "",
    )
    wrong = [i for i, r in enumerate(rows) if r.get("seq") != number(i, len(rows))]
    seq = _check(
        "V09_numbering_contiguous",
        None,
        "fail" if wrong else "pass",
        f"{len(wrong)} finding(s) with a wrong seq" if wrong else "",
    )
    seen: dict[str, int] = {}
    for r in rows:
        t = " ".join(str(r.get("title") or "")[:2000].split()).casefold()
        seen[t] = seen.get(t, 0) + 1
    dups = sorted(t for t, n in seen.items() if n > 1 and t)
    dup = _check(
        "V10_duplicate_title",
        None,
        "warn" if dups else "pass",
        f"{len(dups)} duplicated title(s)" if dups else "",
    )
    return [order, seq, dup]


def verify(
    findings: Sequence[Any], originals: Sequence[Any] | None = None, audit_id: str | None = None
) -> dict[str, Any]:
    """The B8 report: ``{audit_id, normalised, checks, summary}``."""
    rows = [as_mapping(r) for r in findings or ()]
    checks = [c for r in rows for c in _row_checks(r, _original(r, originals))]
    checks += _list_checks(rows)
    summary = {s: sum(1 for c in checks if c["status"] == s) for s in STATUSES}
    return {
        "audit_id": audit_id,
        "normalised": summary["fail"] == 0,
        "checks": checks,
        "summary": summary,
    }
