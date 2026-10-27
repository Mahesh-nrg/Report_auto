"""Finding normalisation (docs/FINDING_ORDER.md part B) on the abstract finding model (B1).

Order per finding (B7 + B9): AI fill of the empty fields (optional ``filler``) -> placeholders
to ``N/A`` -> CWE/CVE ids -> CVSS (vector, number, informational label, LLM ``estimator``,
deterministic heuristic) -> band and mismatch -> informational rule. Then the list is sorted
and numbered (part A). Inputs are never changed.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .cvss import parse_vector
from .finding_estimate import Estimator, Filler, FillResult, run_filler
from .finding_fields import (
    NA,
    TEXT_FIELDS,
    cve_ids,
    cwe_ids,
    fill,
    is_placeholder,
    scan_text,
)
from .finding_order import (
    BANDS,
    as_mapping,
    band_of_label,
    band_of_score,
    is_other_version,
    number,
    numeric_score,
    vector_version,
)
from .finding_tables import CWE_VECTORS, INFORMATIONAL_VECTOR, KEYWORD_RULES, LABEL_DEFAULT_VECTORS

_BY_CWE = {cwe: (vector, score) for cwe, vector, score in CWE_VECTORS}
_RULE_PATTERNS = tuple(
    (
        re.compile("|".join(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])" for k in keywords)),
        cwe,
        vector,
        score,
    )
    for keywords, cwe, vector, score in KEYWORD_RULES
)
Progress = Callable[[int, int], None]


def keyword_rule(finding: Mapping[str, Any]) -> tuple[str, str, float] | None:
    """First keyword rule hit: the title against all rules, then description + observation."""
    title = scan_text(finding.get("title")).lower()
    body = " ".join(scan_text(finding.get(k)) for k in ("description", "observation")).lower()
    for text in (title, body):
        for pattern, cwe, vector, score in _RULE_PATTERNS:
            if pattern.search(text):
                return cwe, vector, score
    return None


def heuristic_estimate(finding: Mapping[str, Any], cwes: list[str]) -> dict[str, Any]:
    """B4 step 5: CWE table -> keyword rules -> label default. Always returns a vector."""
    hit = next((_BY_CWE[c] for c in cwes if c in _BY_CWE), None)
    if hit is not None:
        return {"vector": hit[0], "score": hit[1], "cwe": cwes}
    rule = keyword_rule(finding)
    if rule is not None:
        return {"vector": rule[1], "score": rule[2], "cwe": cwes or [rule[0]]}
    vector, score = LABEL_DEFAULT_VECTORS[band_of_label(finding.get("severity"))]
    return {"vector": vector, "score": score, "cwe": cwes}


def _llm_estimate(estimator: Estimator | None, finding: Mapping) -> dict[str, Any] | None:
    if estimator is None:
        return None
    parsed = parse_vector(estimator(finding))
    if parsed is None:
        return None
    return {"vector": parsed.vector, "score": parsed.score, "source": "estimated_llm"}


def resolve_cvss(
    finding: Mapping[str, Any], cwes: list[str], estimator: Estimator | None = None
) -> dict[str, Any]:
    """B4: the first usable CVSS source, tagged in ``source``."""
    vector = finding.get("cvss_vector")
    parsed = parse_vector(vector)
    if parsed is not None:
        return {"vector": parsed.vector, "score": parsed.score, "source": "vector", "cwe": cwes}
    number_ = None if is_other_version(vector) else numeric_score(finding.get("cvss_score"))
    if number_ is not None:
        return {"vector": NA, "score": number_, "source": "numeric", "cwe": cwes}
    if band_of_label(finding.get("severity")) == "Informational":
        return {
            "vector": INFORMATIONAL_VECTOR,
            "score": 0.0,
            "source": "informational",
            "cwe": cwes,
        }
    estimated = _llm_estimate(estimator, finding)
    if estimated is not None:
        return {**estimated, "cwe": cwes}
    return {**heuristic_estimate(finding, cwes), "source": "estimated_heuristic"}


def empty_fields(finding: Mapping[str, Any]) -> tuple[str, ...]:
    """Fields an AI filler is asked for: placeholders, and the vector when no score is usable."""
    asked = [k for k in TEXT_FIELDS if k != "title" and is_placeholder(finding.get(k))]
    asked += [k for k in ("cwe", "cve") if is_placeholder(finding.get(k))]
    no_score = parse_vector(finding.get("cvss_vector")) is None and (
        is_other_version(finding.get("cvss_vector"))
        or numeric_score(finding.get("cvss_score")) is None
    )
    if no_score and band_of_label(finding.get("severity")) != "Informational":
        asked.append("cvss_vector")
    return tuple(asked)


def _cvss_fields(finding: Mapping[str, Any], cvss: Mapping[str, Any]) -> dict[str, Any]:
    band = band_of_score(cvss["score"])
    info = band == "Informational"
    vector = INFORMATIONAL_VECTOR if info and cvss["vector"] == NA else cvss["vector"]
    raw_vector = finding.get("cvss_vector")
    label = band_of_label(finding.get("severity"))
    cves = cve_ids(finding)
    return {
        "severity": band,
        "cvss_score": cvss["score"],
        "cvss_vector": vector,
        "cvss_source": cvss["source"],
        "cvss_estimated": cvss["source"] in ("estimated_heuristic", "estimated_llm", "llm"),
        "cvss_v4_vector": raw_vector.strip() if vector_version(raw_vector) == "4" else NA,
        "cwe": NA if info or not cvss["cwe"] else ", ".join(cvss["cwe"]),
        "cve": NA if info or not cves else ", ".join(cves),
        "mismatch": label != "Unknown" and label != band,
    }


def _with_fill(finding: Mapping[str, Any], filled: FillResult) -> Mapping[str, Any]:
    return {**finding, **filled.values} if filled.values else finding


def _ai_cvss(finding: Mapping[str, Any], filled: FillResult, cwes: list[str]) -> dict | None:
    parsed = parse_vector(filled.values.get("cvss_vector"))
    if parsed is None:
        return None
    return {"vector": parsed.vector, "score": parsed.score, "source": "llm", "cwe": cwes}


def _changes(before: Mapping[str, Any], after: Mapping[str, Any], ai: set[str]) -> list[dict]:
    out = []
    for key in (*TEXT_FIELDS, "severity", "cvss_score", "cvss_vector", "cwe", "cve"):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        source = "ai" if key in ai else ("na" if new == NA else "computed")
        out.append({"field": key, "before": old, "after": new, "source": source})
    return out


def normalize_finding(
    finding: Any, estimator: Estimator | None = None, filler: Filler | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One finding of the abstract model -> (normalised copy without ``seq``, changes)."""
    original = as_mapping(finding)
    filled = run_filler(filler, original, empty_fields(original))
    merged = _with_fill(original, filled)
    cwes = cwe_ids(merged)
    cvss = _ai_cvss(merged, filled, cwes) or resolve_cvss(merged, cwes, estimator)
    out = {
        **{k: fill(merged.get(k)) for k in TEXT_FIELDS},
        **_cvss_fields(merged, cvss),
        "ai_filled": [k for k in filled.values if k != "cvss_vector" or cvss["source"] == "llm"],
        "ai_sources": {k: list(v) for k, v in filled.sources.items() if v},
    }
    if out["severity"] == "Informational":  # B5: N/A ids even when the AI named some
        out["ai_filled"] = [k for k in out["ai_filled"] if k not in ("cwe", "cve")]
    return out, _changes(original, out, set(out["ai_filled"]))


def _usable_index(index: Any, position: int) -> int:
    return index if isinstance(index, int) and not isinstance(index, bool) else position


def _key(row: Mapping[str, Any], index: int, position: int) -> tuple:
    return (BANDS.index(row["severity"]), -row["cvss_score"], index, position)


def normalize_audit(
    findings: Sequence[Any],
    estimator: Estimator | None = None,
    progress: Progress | None = None,
    filler: Filler | None = None,
    audit_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Normalise, sort and number a list. Returns (ordered rows, changes, verification).

    Each row carries ``position`` (its place in the input list), ``index`` (the sort index:
    the input's ``index`` when it is an int, else ``position``) and ``seq``; each change entry
    is ``{position, seq, changes: [...]}``.
    """
    from .finding_verify import verify  # local import: verify depends on this module

    items = list(findings or ())
    rows = []
    for position, item in enumerate(items):
        row, changes = normalize_finding(item, estimator, filler)
        index = _usable_index(as_mapping(item).get("index"), position)
        extra = {"index": index, "position": position}
        rows.append((_key(row, index, position), {**row, **extra}, changes))
        if progress is not None:
            progress(position + 1, len(items))
    rows.sort(key=lambda r: r[0])
    count = len(rows)
    ordered = [{"seq": number(i, count), **row} for i, (_, row, _) in enumerate(rows)]
    changes = [
        {"position": row["position"], "seq": row["seq"], "changes": ch}
        for row, (_, _, ch) in zip(ordered, rows, strict=True)
    ]
    return ordered, changes, verify(ordered, originals=items, audit_id=audit_id)
