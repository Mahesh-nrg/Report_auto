"""PwnDoc tag vocabulary of the ParadigmIT VAPT template (PwnDoc 0.5.x container generator).

Every audit-specific value of deliverable 1 is replaced by one of these docxtemplater tags.
Syntax = PwnDoc's engine: angular-expressions parser, ``paragraphLoop: true``, ``{#x}``/``{/x}``
sections, ``{^x}`` inverted sections, ``{@x}`` raw XML, ``{%%x}`` centred image (image module).
Data model = report-generator.js prepAuditData: finding custom fields become keys named
``deburr(label.lower()).replace(/\\s/g,'').replace(/[^\\w]/g,'_')`` (``Vulnerable URL`` ->
``vulnerableurl``), audit custom fields the same without the ``[^\\w]`` step, custom sections
``result[section.field]``.
"""

from __future__ import annotations

import copy

from pd_meta import EXAMPLE_META

SEVERITIES = ("Critical", "High", "Medium", "Low", "Informational")
RICH_FILTER = "paradigmHTML"  # custom filter (report-filters-custom.js), see PWNDOC_TEMPLATE_GUIDE


# Every findings loop/count goes through the custom filters of report-filters-custom.js, which
# implement docs/FINDING_ORDER.md part A: order by CVSS v3.1 score (Critical -> Informational),
# seq 001.., severity band DERIVED from the score (orderBand), render-time N/A fill.
LOOP = "findings | sortFindings"
OTHER_BANDS = "Medium,Low,Informational,Unknown"


def sev_is(severity: str, quote: str = '"') -> str:
    """Condition matching a sorted finding's DERIVED band (``orderBand`` from sortFindings)."""
    return f"orderBand == {quote}{severity}{quote}"


PRIORITY_BANDS = "Critical,High"
UNKNOWN_SEV = "orderBand == 'Unknown'"


def count(bands: str, pad: bool = True) -> str:
    """Number of findings whose derived band is in ``bands`` (``padNumber``: 4 -> "04")."""
    tail = " | padNumber | d:'00'" if pad else ""
    return f"{{findings | bandCount:'{bands}'{tail}}}"


def in_band(bands: str) -> str:
    return f"findings | inBand:'{bands}'"


def titles(bands: str, fallback: str) -> str:
    """Inline loop: the titles of the findings of ``bands`` in report order, or ``fallback``."""
    expr = in_band(bands)
    return f"{{#{expr}}}{{title}}. {{/{expr}}}{{^{expr}}}{fallback}{{/{expr}}}"


TOTAL = "{findings | length}"
CLIENT = "{company.name | d:'Client'}"
APPLICATION = "{applicationname | d:name}"
ASSESSMENT = "{assessmenttype | d:'Grey-box'}"
URL = "{targeturl | d:(scope | select:'name' | join:', ') | d:'N/A'}"
START = "{date_start | convertDateLocale:'en-GB' | d:'TBD'}"
END = "{date_end | convertDateLocale:'en-GB' | d:'TBD'}"
WINDOW = f"{START} - {END}"
REPORT_TITLE = "{name | upper}"

TAG_TEXT: dict = {
    "client": CLIENT,
    "application": APPLICATION,
    "assessment_short": ASSESSMENT,
    "cover_meta": "{assessmenttype | d:'Grey-box' | upper}  /  WEB APPLICATION",
    "test_type": f"{ASSESSMENT} penetration test",
    "url": URL,
    "window": WINDOW,
    "window_upper": WINDOW,
    "total": TOTAL,
    "priority": count(PRIORITY_BANDS),
    "other": count(OTHER_BANDS),
    "priority_lines": (
        f"{count('Critical', pad=False)} Critical",
        f"{count('High', pad=False)} High",
    ),
    "priority_note": "Prioritise the highest-rated findings first.",
    "kicker_p3": (
        "01  /  {assessmenttype | d:'Grey-box' | upper} PENETRATION TEST   /   "
        "{applicationname | d:name | upper}"
    ),
    "exec_summary": (
        f"ParadigmIT Cybersecurity assessed the {CLIENT} web application to identify "
        "weaknesses that could permit unauthorised access or exposure of sensitive information. "
        "The findings and recommendations in this document support risk-based remediation and "
        "constitute the report of the completed assessment."
    ),
    "remediation_lead": (
        f"The assessment identified {TOTAL} findings across five severity levels. The following "
        "sequence is proposed as a management follow-up to the documented technical "
        "recommendations; it is not a statement that remediation has occurred."
    ),
    "priority_1": titles("Critical", "No critical-severity findings were identified."),
    "priority_2": titles("High", "No high-severity findings were identified."),
    "findings_intro": (
        "Assessment evidence, reproduction steps, recommendations and references "
        f"for all {TOTAL} findings."
    ),
}

FOOTER_B = f"CONFIDENTIAL   /   {REPORT_TITLE}   /   {CLIENT}"

# ---------------------------------------------------------------- per finding

FINDING_ID = "{seq}"  # 001.. in report order (sortFindings), not the stored identifier
FINDING_HEADING = f"Finding {FINDING_ID} — {{title}}"
FIELDS = {
    "location": "{vulnerablelocation | d:'N/A'}",
    "url": "{vulnerableurl | d:'N/A'}",
    "parameter": "{vulnerableparameter | d:'N/A'}",
    "cwe": "{cwecve | d:'N/A'}",
    "cvss": "{scoreText}{#cvssEstimated} ({estimatedNote}){/cvssEstimated}",
    "severity_any": "{displaySeverity}",
}
REGISTER_LOC = (
    "{vulnerableurl | d:'N/A'}",
    "Param: {vulnerableparameter | d:'N/A'}",
    "Location: {vulnerablelocation | d:'N/A'}",
)


def rich(style: str) -> str:
    """Raw-XML tag rendering one HTML chunk ``text`` with paragraph style ``style``."""
    return f"{{@text | {RICH_FILTER}:'{style}'}}"


def tag_meta() -> dict:
    """Deliverable-1 meta with every audit-specific value replaced by its tag."""
    meta = copy.deepcopy(EXAMPLE_META)
    meta.update(copy.deepcopy(TAG_TEXT))
    meta["counts"] = dict.fromkeys(SEVERITIES, 1)  # equal-width severity key bar
    return meta
