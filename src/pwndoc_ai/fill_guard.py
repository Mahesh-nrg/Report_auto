"""Acceptance rules for the refiner's ``fill_missing`` mode (docs/FINDING_ORDER.md, B9).

The model is asked only for empty fields; these checks drop any value that is not grounded:

- references: a URL is kept only when the research we fetched names it (the URL text itself, or
  its host is one of the research sources); nothing else is ever kept;
- CVE ids in custom fields: kept only when the finding text or the research names them;
- vulnerable location / URL / parameter: kept only when the finding text already states it
  (never a guessed host, IP or parameter);
- CWE: must look like ``CWE-<digits>`` (or ``N/A``).

A field that fails is removed from the payload and from the review diff, so it stays empty and
the deterministic fallback (heuristic / ``N/A``) applies later.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from .finding_adapter import field_kind, plain

PLACEHOLDERS = frozenset(
    {"", "-", "--", "---", "–", "—", "none", "null", "nil", "n/a", "na", "n.a.",
     "tbd", "undefined", "not applicable"}
)  # fmt: skip
CVE = re.compile(r"CVE-\d{4}-\d{4,7}", re.IGNORECASE)
CWE_VALUE = re.compile(r"^(N/A|CWE-\d+(\s*,\s*CWE-\d+)*)", re.IGNORECASE)
STATED_KINDS = ("location", "path", "parameter")
CVE_KINDS = ("cve", "cwecve")
FINDING_TEXT_KEYS = ("title", "description", "observation", "poc", "scope", "remediation")


def is_placeholder(value: Any) -> bool:
    """B2: missing, or text that only says 'none', '-', 'N/A'... Images count as content."""
    if isinstance(value, list):
        return all(is_placeholder(v) for v in value)
    if not isinstance(value, str):
        return value is None
    if "<img" in value:
        return False
    return plain(value).lower() in PLACEHOLDERS


def finding_text(finding: Mapping[str, Any]) -> str:
    parts = [plain(finding.get(k)) for k in FINDING_TEXT_KEYS]
    parts += [
        plain(e.get("text")) for e in finding.get("customFields") or [] if isinstance(e, Mapping)
    ]
    return "\n".join(parts).lower()


def grounded_reference(ref: str, research: str, sources: Iterable[str]) -> bool:
    if ref in research:
        return True
    host = (urlsplit(ref).hostname or "").lower()
    return bool(host) and host in {s.lower() for s in sources}


def _custom_ok(entry: Mapping[str, Any], stated: str, known: str) -> bool:
    kind = field_kind(entry)
    value = plain(entry.get("text"))
    if kind in STATED_KINDS:
        return value.lower() in stated
    if kind in CVE_KINDS and any(c.lower() not in known for c in CVE.findall(value)):
        return False
    if kind == "cwe":
        return bool(CWE_VALUE.match(value))
    return True


def _guard_custom(
    payload: dict[str, Any], changes: dict[str, Any], finding: Mapping[str, Any], known: str
) -> None:
    stated = finding_text(finding)
    old_by_label = {
        plain((e.get("customField") or {}).get("label")): e
        for e in finding.get("customFields") or []
        if isinstance(e, Mapping)
    }
    kept = []
    for entry in payload.get("customFields") or []:
        label = plain((entry.get("customField") or {}).get("label"))
        key = f"customFields: {label}"
        if key in changes and not _custom_ok(entry, stated, known):
            changes.pop(key)
            entry = old_by_label.get(label, entry)
        kept.append(entry)
    if not any(k.startswith("customFields: ") for k in changes):
        payload.pop("customFields", None)
    elif kept:
        payload["customFields"] = kept


def guard_fill(
    finding: Mapping[str, Any],
    payload: Mapping[str, Any],
    changes: Mapping[str, Any],
    research: str,
    sources: Iterable[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """New payload and changes with every ungrounded AI value removed (inputs untouched)."""
    new_payload = dict(payload)
    new_changes = dict(changes)
    known = finding_text(finding) + "\n" + research.lower()
    if "references" in new_payload:
        refs = [r for r in new_payload["references"] if grounded_reference(r, research, sources)]
        if refs:
            new_payload["references"] = refs
            new_changes["references"] = {"before": "", "after": "\n".join(refs)}
        else:
            new_payload.pop("references")
            new_changes.pop("references", None)
    _guard_custom(new_payload, new_changes, finding, known)
    return new_payload, new_changes
