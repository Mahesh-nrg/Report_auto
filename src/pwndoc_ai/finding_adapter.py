"""Map PwnDoc findings to the abstract finding model of docs/FINDING_ORDER.md (B1) and back.

``to_abstract`` reads a PwnDoc finding (built-in keys plus custom fields found by label) into the
plain dict that the normaliser works on. ``write_back`` turns a normalised row into the PUT payload
PwnDoc accepts (only changed keys) plus a human-readable ``changes`` map for the review screen.
Nothing here mutates its inputs.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from typing import Any

from .cvss import is_score_label, is_severity_label

NA = "N/A"
NA_HTML = "<p>N/A</p>"
RICH_KEYS = ("description", "observation", "poc", "remediation")
# abstract key -> normalised custom-field labels (lower case, letters and digits only)
CUSTOM_LABELS: dict[str, frozenset[str]] = {
    "cwe": frozenset({"cwe", "cweid"}),
    "cve": frozenset({"cve", "cveid"}),
    "cwecve": frozenset({"cwecve", "cweandcve"}),
    "location": frozenset({"vulnerablelocation", "location"}),
    "path": frozenset({"vulnerableurl", "pathurl", "url", "path", "vulnerablepath"}),
    "parameter": frozenset({"vulnerableparameter", "parameter"}),
    "steps": frozenset({"stepstoreproduce", "steps"}),
    "source": frozenset({"cvsssource", "cvssestimated"}),
}
ESTIMATED_SOURCE = re.compile(r"estimat|llm|heuristic", re.IGNORECASE)
INFO_OPTIONS = ("informational", "info", "none")
_NON_ALNUM = re.compile(r"[^a-z0-9]")
_TAG = re.compile(r"<[^>]*>")


def label_key(label: Any) -> str:
    return _NON_ALNUM.sub("", str(label or "").lower())


def _custom(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    custom = entry.get("customField")
    return custom if isinstance(custom, Mapping) else {}


def _label(entry: Mapping[str, Any]) -> str:
    custom = _custom(entry)
    return str(custom.get("label") or custom.get("display") or "")


def field_kind(entry: Mapping[str, Any]) -> str | None:
    """The abstract role of a custom-field entry (``score``, ``severity``, ``cwe``...) or None."""
    label = _label(entry)
    if is_score_label(label):
        return "score"
    if is_severity_label(label):
        return "severity"
    key = label_key(label)
    return next((kind for kind, names in CUSTOM_LABELS.items() if key in names), None)


def _entries(finding: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [e for e in finding.get("customFields") or [] if isinstance(e, Mapping)]


def custom_values(finding: Mapping[str, Any]) -> dict[str, Any]:
    """First value per abstract role among the finding's custom fields."""
    values: dict[str, Any] = {}
    for entry in _entries(finding):
        kind = field_kind(entry)
        if kind and kind not in values:
            values[kind] = entry.get("text")
    return values


def _source_text(row: Mapping[str, Any]) -> str | None:
    """'vector' / 'numeric' ..., or 'estimated_x <vector>' so a later user edit is detectable."""
    source = str(row.get("cvss_source") or "")
    if not source:
        return None
    if row.get("cvss_estimated") and row.get("cvss_vector") not in (None, NA):
        return (
            source if source.endswith(str(row["cvss_vector"])) else f"{source} {row['cvss_vector']}"
        )
    return source


def is_estimated_source(value: Any) -> bool:
    """True when a stored 'CVSS Source' text marks the vector as estimated (AI/heuristic)."""
    return bool(ESTIMATED_SOURCE.search(plain(value)))


def _references(value: Any) -> str | None:
    if not isinstance(value, list):
        return value if isinstance(value, str) else None
    return "\n".join(str(v) for v in value if isinstance(v, str))


def to_abstract(finding: Mapping[str, Any], index: int) -> dict[str, Any]:
    """The abstract model (B1) of one PwnDoc finding; ``index`` is its stored position."""
    custom = custom_values(finding)
    combined = custom.get("cwecve")
    return {
        "title": finding.get("title"),
        "cvss_vector": finding.get("cvssv3") or None,
        "cvss_score": custom.get("score", finding.get("cvssScore")),
        "severity": custom.get("severity", finding.get("severity")),
        "cwe": custom.get("cwe", combined),
        "cve": custom.get("cve", combined),
        "location": custom.get("location"),
        "path": custom.get("path"),
        "parameter": custom.get("parameter"),
        "description": finding.get("description"),
        "observation": finding.get("observation"),
        "poc": finding.get("poc"),
        "steps": custom.get("steps"),
        "remediation": finding.get("remediation"),
        "references": _references(finding.get("references")),
        "affected": finding.get("scope"),
        "cvss_source_stored": plain(custom.get("source")) or None,
        "index": index,
    }


def plain(value: Any) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", str(value or ""))).split())


def _option_for(entry: Mapping[str, Any], value: str) -> str | None:
    """A select field's option matching ``value`` (Informational may be Info/None there)."""
    options = [str(o.get("value", "")) for o in _custom(entry).get("options") or [] if o]
    if not options:
        return value
    by_lower = {o.lower(): o for o in options}
    wanted = INFO_OPTIONS if value == "Informational" else (value.lower(),)
    return next((by_lower[w] for w in wanted if w in by_lower), None)


def _cwecve(row: Mapping[str, Any]) -> str:
    parts = [row.get("cwe"), row.get("cve")]
    real = [str(p) for p in parts if p and p != NA]
    return " / ".join(real) if real else NA


def _custom_target(entry: Mapping[str, Any], row: Mapping[str, Any]) -> str | None:
    kind = field_kind(entry)
    if kind is None:
        return None
    if kind == "score":
        return f"{float(row['cvss_score']):.1f}"
    if kind == "severity":
        return _option_for(entry, str(row["severity"]))
    if kind == "source":
        return _source_text(row)
    value = _cwecve(row) if kind == "cwecve" else row.get(kind)
    if not isinstance(value, str):
        return None
    if value == NA and _custom(entry).get("fieldType") == "text":
        return NA_HTML
    return value


def _merge_custom(
    finding: Mapping[str, Any], row: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    merged: list[dict[str, Any]] = []
    changes: dict[str, dict[str, str]] = {}
    for entry in _entries(finding):
        target = _custom_target(entry, row)
        before = entry.get("text")
        if target is None or plain(target) == plain(before):
            merged.append(dict(entry))
            continue
        changes[f"customFields: {_label(entry)}"] = {
            "before": plain(before),
            "after": plain(target),
        }
        merged.append({**entry, "text": target})
    return merged, changes


def _builtin_targets(row: Mapping[str, Any]) -> dict[str, Any]:
    targets: dict[str, Any] = {"title": row.get("title"), "scope": row.get("affected")}
    targets.update({k: row.get(k) for k in RICH_KEYS})
    return targets


def _builtin_changes(
    finding: Mapping[str, Any], row: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    payload: dict[str, Any] = {}
    changes: dict[str, dict[str, str]] = {}
    for key, value in _builtin_targets(row).items():
        if value != NA or plain(finding.get(key)) == NA:
            continue  # only placeholders become N/A; real text is never rewritten here
        payload[key] = NA if key == "title" else NA_HTML
        changes[key] = {"before": plain(finding.get(key)), "after": NA}
    if row.get("references") == NA and not _real_refs(finding.get("references")):
        if finding.get("references") != [NA]:
            payload["references"] = [NA]
            changes["references"] = {"before": "", "after": NA}
    vector = row.get("cvss_vector")
    if isinstance(vector, str) and vector != NA and vector != finding.get("cvssv3"):
        payload["cvssv3"] = vector
        changes["cvssv3"] = {"before": str(finding.get("cvssv3") or ""), "after": vector}
    return payload, changes


def _real_refs(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and plain(v) and plain(v).lower() != "n/a"]


def write_back(
    finding: Mapping[str, Any], row: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """PUT payload (changed keys only) and the review diff that bring ``finding`` to ``row``."""
    payload, changes = _builtin_changes(finding, row)
    merged, custom_changes = _merge_custom(finding, row)
    if custom_changes:
        payload["customFields"] = merged
        changes.update(custom_changes)
    return payload, changes


def absent_fields(finding: Mapping[str, Any]) -> list[str]:
    """Abstract roles that the audit type defines no custom field for (B1 ``field_absent``)."""
    present = {field_kind(e) for e in _entries(finding)}
    if "cwecve" in present:
        present |= {"cwe", "cve"}
    roles = ("score", "severity", "cwe", "cve", "location", "path", "parameter", "steps")
    return [r for r in roles if r not in present]
