"""Field helpers shared by normalisation, AI-fill validation and verification
(docs/FINDING_ORDER.md B1-B3): the abstract text fields, the placeholder rule and the CWE/CVE
id extraction. Pure functions; scanned text is bounded so hostile input stays cheap."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

NA = "N/A"
TEXT_FIELDS = (
    "title",
    "location",
    "path",
    "parameter",
    "description",
    "observation",
    "poc",
    "steps",
    "remediation",
    "references",
    "affected",
)
RICH_FIELDS = ("description", "observation", "poc", "steps", "remediation")
PLACEHOLDERS = frozenset(
    {
        "",
        "-",
        "--",
        "---",
        "–",
        "—",
        "none",
        "null",
        "nil",
        "n/a",
        "na",
        "n.a.",
        "tbd",
        "undefined",
        "not applicable",
    }
)
MAX_SCAN = 100_000  # characters of one field that are scanned for ids / keywords
_TAG = re.compile(r"<[^>]*>")
CWE_RE = re.compile(r"(?<![A-Za-z0-9])CWE[\s:_-]*(\d{1,5})(?![0-9])", re.IGNORECASE)
CVE_RE = re.compile(r"(?<![A-Za-z0-9])CVE-(\d{4})-(\d{4,7})(?![0-9])", re.IGNORECASE)
CWE_LIST = re.compile(r"^CWE-\d+(?:, CWE-\d+)*$")
CVE_LIST = re.compile(r"^CVE-\d{4}-\d{4,7}(?:, CVE-\d{4}-\d{4,7})*$")
_BARE_ID = re.compile(r"\d{1,5}")


def scan_text(value: Any) -> str:
    return value[:MAX_SCAN] if isinstance(value, str) else ""


def is_placeholder(value: Any) -> bool:
    """B2: not a string, or empty/dash/none/n-a... after tag removal and whitespace collapse."""
    if not isinstance(value, str):
        return True
    if len(value) > 4 * MAX_SCAN:
        return False  # far too long to be a placeholder word
    text = _TAG.sub(" ", value).replace("&nbsp;", " ").replace(" ", " ")
    return " ".join(text.split()).lower() in PLACEHOLDERS


def fill(value: Any) -> Any:
    return NA if is_placeholder(value) else value


def distinct(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def cwe_in(text: Any) -> list[str]:
    return distinct(f"CWE-{int(n)}" for n in CWE_RE.findall(scan_text(text)))


def cve_in(text: Any) -> list[str]:
    return distinct(f"CVE-{y}-{n}" for y, n in CVE_RE.findall(scan_text(text)))


def cwe_ids(finding: Mapping[str, Any]) -> list[str]:
    """B3: ids from the ``cwe`` field (or bare digits), else explicit mentions in the text."""
    raw = finding.get("cwe")
    if isinstance(raw, str):
        found = cwe_in(raw)
        if not found and _BARE_ID.fullmatch(raw.strip()):
            found = [f"CWE-{int(raw.strip())}"]
        if found:
            return found
    keys = ("title", "description", "observation", "references")
    return distinct(c for k in keys for c in cwe_in(finding.get(k)))


def cve_ids(finding: Mapping[str, Any]) -> list[str]:
    """B3: the ``cve`` field first, else the finding text. Never invented."""
    from_field = cve_in(finding.get("cve"))
    if from_field:
        return from_field
    keys = ("title", "description", "observation", "poc", "references")
    return distinct(c for k in keys for c in cve_in(finding.get(k)))


def finding_text(finding: Mapping[str, Any]) -> str:
    """All string values of the finding, for 'stated in the finding' checks."""
    return "\n".join(scan_text(v) for v in finding.values() if isinstance(v, str))
