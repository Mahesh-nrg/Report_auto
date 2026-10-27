"""docs/FINDING_ORDER.md part B for imported findings (deterministic last fallback).

Runs on the plan after the importer's LLM fallback (which fills gaps first, B9) and before the
Part A sort, so the preview, the created audit and the gateway agree:

* no usable v3.1 score -> the shared normaliser computes or estimates a vector (CWE table, keyword
  rules, label default) and the finding is flagged ``cvss_estimated`` for review;
* the severity becomes the band of the v3.1 score; Informational -> 0.0, CWE ``N/A``;
* CWE must be ``CWE-<n>`` (or is derived from the text, else ``N/A``); a CVE is never invented;
* empty parameter / location become ``N/A``, empty rich text ``<p>N/A</p>``.

Nothing here mutates its input; an unavailable normaliser leaves the finding unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from . import ordering
from .model import ImportedFinding

LOG = logging.getLogger(__name__)
NA = "N/A"
NA_HTML = "<p>N/A</p>"
ESTIMATED_FLAG = "cvss_estimated"
RICH = ("description", "observation", "remediation")
PLAIN = ("parameter", "location")


def _abstract(finding: ImportedFinding, index: int) -> dict[str, Any]:
    return {
        "title": finding.title,
        "cvss_vector": finding.cvss_vector or None,
        "cvss_score": finding.cvss_score or None,
        "severity": finding.severity or None,
        "cwe": finding.cwe or None,
        "cve": None,
        "description": finding.description,
        "observation": finding.observation,
        "remediation": finding.remediation,
        "parameter": finding.parameter,
        "location": finding.location,
        "index": index,
    }


def _cvss_changes(finding: ImportedFinding, row: dict[str, Any]) -> dict[str, Any]:
    if ordering.finding_score(finding) is not None:
        return {}
    vector = row.get("cvss_vector")
    if not isinstance(vector, str) or not vector.startswith("CVSS:3"):
        return {}
    score = row.get("cvss_score")
    return {"cvss_vector": vector, "cvss_score": f"{float(score):.1f}" if score is not None else ""}


def _text_changes(finding: ImportedFinding, row: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for key in RICH:
        if row.get(key) == NA and getattr(finding, key) != NA_HTML:
            changes[key] = NA_HTML
    for key in PLAIN:
        if row.get(key) == NA and getattr(finding, key) != NA:
            changes[key] = NA
    cwe = row.get("cwe")
    if isinstance(cwe, str) and cwe != finding.cwe and (cwe == NA or cwe.startswith("CWE-")):
        changes["cwe"] = cwe
    return changes


def normalize_imported(finding: ImportedFinding, index: int = 0) -> ImportedFinding:
    """One finding with Part B applied (a new object; the input is unchanged)."""
    from pwndoc_ai.finding_normalize import normalize_finding

    try:
        row, _ = normalize_finding(_abstract(finding, index))
    except Exception:  # the shared normaliser must never break an import
        LOG.exception("finding normalisation failed for %s", finding.key)
        return finding
    changes = {**_cvss_changes(finding, row), **_text_changes(finding, row)}
    estimated = bool(row.get("cvss_estimated")) and "cvss_vector" in changes
    if estimated and ESTIMATED_FLAG not in finding.flags:
        changes["flags"] = (*finding.flags, ESTIMATED_FLAG)
    updated = replace(finding, **changes) if changes else finding
    severity = ordering.derived_severity(updated)
    if severity and severity != updated.severity:
        updated = replace(updated, severity=severity)
    return updated


def normalize_findings(findings: Sequence[ImportedFinding]) -> tuple[ImportedFinding, ...]:
    """Part B for every finding, then the Part A order (stable on plan order)."""
    done = tuple(normalize_imported(f, i) for i, f in enumerate(findings))
    return ordering.sort_findings(done)


def plan_order(findings: Sequence[ImportedFinding]) -> tuple[ImportedFinding, ...]:
    """The unchanged findings in the order their normalised versions will be created.

    The preview keeps the extracted values (so completeness still reports the gaps) while its
    S. No. already equals the PwnDoc identifier the writer will produce.
    """
    by_key = {f.key: f for f in findings}
    return tuple(by_key[f.key] for f in normalize_findings(findings))
