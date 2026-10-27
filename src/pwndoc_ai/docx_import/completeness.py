"""Completeness panel of the import preview: every issue derived from the plan itself.

``assess`` recomputes the list from scratch on every build and edit, so the panel always matches
what will be written. Only a few codes cannot be derived from plan data and are carried over from
the previous list (``STICKY`` and the header-level ``CARRIED`` codes). Output order is fixed:
header and source issues, then findings in plan order, then images in plan order.

The reader's text helpers (``html.plain_text``, ``labels.name_key``) are imported at call time so
this module never pulls in the XML stack at import.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import date

from .. import cvss
from . import limits, ordering
from .model import CompletenessIssue, ImportedFinding, ImportedImage, ImportPlan

STICKY = frozenset({"llm_unavailable", "llm_failed", "llm_skipped_unmasked"})
# Header-level issues (no finding or image key) that only the reader or the fallback can know.
CARRIED = frozenset({"text_truncated", "llm_fields"})
# Finding fields whose blank value is reported under ``fields_blank``.
CHECKED_FIELDS = ("description", "poc", "remediation")
IMAGE_FLAG_CODES = (
    ("unmatched", "image_unmatched"),
    ("placeholder", "image_placeholder"),
    ("unsupported", "image_unsupported"),
    ("too_large", "image_too_large"),
)


def _plain(value: str) -> str:
    from . import html as html_tools

    return html_tools.plain_text(value) if value else ""


def _key(value: str) -> str:
    from . import labels

    return labels.name_key(value)


def leftover_keys() -> frozenset[str]:
    """Name keys of the company names the active template hardcodes in its body text (the
    configurable ``labels.TEMPLATE_LEFTOVER_NAMES``). A company equal to one of them is worth a
    second look even when the vote chose it."""
    from . import labels

    return frozenset(labels.name_key(name) for name in labels.TEMPLATE_LEFTOVER_NAMES)


def _blank(value: str) -> bool:
    from . import labels

    return labels.is_blank(value)


def _issue(code: str, finding: str = "", image: str = "", fields: Iterable[str] = ()):
    return CompletenessIssue(code, finding_key=finding, image_key=image, fields=tuple(fields))


def _option_ok(value: str, choices: Sequence[str]) -> bool:
    return bool(value) and (not choices or value in choices)


def _header_choices(plan: ImportPlan) -> list[CompletenessIssue]:
    header, options = plan.header, plan.options
    out = []
    if _blank(header.company):
        out.append(_issue("company_missing", fields=("company",)))
    if _blank(header.audit_name):
        out.append(_issue("audit_name_missing", fields=("audit_name",)))
    if not _option_ok(header.language, [c.value for c in options.languages]):
        out.append(_issue("language_missing", fields=("language",)))
    if not _option_ok(header.audit_type, [a.name for a in options.audit_types]):
        out.append(_issue("audit_type_missing", fields=("audit_type",)))
    if not _option_ok(header.template_id, [c.value for c in options.templates]):
        out.append(_issue("template_missing", fields=("template_id",)))
    return out


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if limits.ISO_DATE.fullmatch(value) else None
    except ValueError:
        return None


def _header_dates(plan: ImportPlan) -> list[CompletenessIssue]:
    values = {"date_start": plan.header.date_start, "date_end": plan.header.date_end}
    parsed = {name: _parse_date(value) for name, value in values.items() if value}
    bad = [name for name, value in parsed.items() if value is None]
    out = []
    if bad:
        out.append(_issue("date_invalid", fields=bad))
    elif len(parsed) == 2 and parsed["date_start"] > parsed["date_end"]:  # type: ignore[operator]
        out.append(_issue("date_order", fields=("date_start", "date_end")))
    missing = [name for name, value in values.items() if not value]
    if missing:
        out.append(_issue("dates_missing", fields=missing))
    if not plan.header.scope:
        out.append(_issue("scope_missing", fields=("scope",)))
    if plan.header.contact is None:
        out.append(_issue("contacts_absent", fields=("contact",)))
    return out


def _company_source(plan: ImportPlan) -> list[CompletenessIssue]:
    source, company = plan.source_header, _key(plan.header.company)
    out = []
    candidates = {_key(name) for name in source.company_candidates if name.strip()}
    if len(candidates) > 1:
        out.append(_issue("company_conflict", fields=("company",)))
    others = [name for name in source.leftover_names if _key(name) != company]
    if others or (company and company in leftover_keys()):
        out.append(_issue("leftover_name", fields=("company",)))
    if company:
        out.append(_issue("company_existing" if plan.company_id else "company_new"))
    if plan.duplicate_audit:
        out.append(_issue("duplicate_audit", fields=("audit_name",)))
    if plan.previous_import:
        out.append(_issue("previous_import"))
    return out


def _severity_mismatch(plan: ImportPlan) -> bool:
    printed = dict(plan.source_header.severity_table)
    if not printed:
        return False
    counted = Counter(f.severity for f in plan.findings if f.severity)
    return any(counted.get(name, 0) != count for name, count in printed.items())


def _source_stats(plan: ImportPlan) -> list[CompletenessIssue]:
    stats, out = plan.stats, []
    if _severity_mismatch(plan):
        out.append(_issue("severity_table_mismatch", fields=("severity",)))
    if stats.id_gaps:
        out.append(_issue("id_gaps"))
    if stats.merged:
        out.append(_issue("duplicates_merged"))
    if stats.figures_deduped:
        out.append(_issue("figures_deduped"))
    return out


def _carried(previous: Iterable[CompletenessIssue]) -> list[CompletenessIssue]:
    return [
        issue
        for issue in previous
        if issue.code in STICKY
        or (issue.code in CARRIED and not issue.finding_key and not issue.image_key)
    ]


def _poc_blank(finding: ImportedFinding, attached: set[str]) -> bool:
    if any(block.kind == "image" for block in finding.poc) or finding.key in attached:
        return False
    return not any(_plain(block.html).strip() for block in finding.poc if block.kind == "html")


def _blank_fields(finding: ImportedFinding, attached: set[str]) -> list[str]:
    blank = [name for name in ("description", "remediation") if not _plain(getattr(finding, name))]
    if _poc_blank(finding, attached):
        blank.append("poc")
    return [name for name in CHECKED_FIELDS if name in blank]


def _finding_issues(finding: ImportedFinding, attached: set[str]) -> list[CompletenessIssue]:
    key, out = finding.key, []
    if _blank(finding.title):
        out.append(_issue("title_missing", key, fields=("title",)))
    if not finding.severity:
        out.append(_issue("severity_missing", key, fields=("severity",)))
    if cvss.parse_vector(finding.cvss_vector) is None:  # blank, or not a valid v3 vector
        out.append(_issue("cvss_missing", key, fields=("cvss_vector",)))
    if ordering.is_mismatch(finding):  # old -> new is in the verification report's note
        out.append(_issue("severity_label_mismatch", key, fields=("severity",)))
    blank = _blank_fields(finding, attached)
    if blank:
        out.append(_issue("fields_blank", key, fields=blank))
    if "summary_only" in finding.flags:
        out.append(_issue("summary_only", key))
    if "copies_disagree" in finding.flags:
        out.append(_issue("copies_disagree", key, fields=("severity", "cwe", "cvss_vector")))
    if "truncated" in finding.flags:
        out.append(_issue("text_truncated", key))
    if finding.llm_fields:
        out.append(_issue("llm_fields", key, fields=finding.llm_fields))
    return out


def _image_issues(image: ImportedImage) -> list[CompletenessIssue]:
    flags = set(image.flags)
    out = [
        _issue(code, image.finding_key, image.key)
        for flag, code in IMAGE_FLAG_CODES
        if flag in flags
    ]
    if image.include and ("uncaptioned" in flags or not image.caption.strip()):
        out.append(_issue("image_uncaptioned", image.finding_key, image.key, ("images",)))
    return out


def assess(plan: ImportPlan) -> tuple[CompletenessIssue, ...]:
    """Every completeness issue of ``plan``, deterministic and deduplicated."""
    included = [f for f in plan.findings if f.include]
    attached = {i.finding_key for i in plan.images if i.include and i.usable and i.finding_key}
    issues = _header_choices(plan) + _header_dates(plan) + _company_source(plan)
    issues += _source_stats(plan) + _carried(plan.issues)
    if not included:
        issues.append(_issue("no_findings"))
    for finding in included:
        issues += _finding_issues(finding, attached)
    for image in plan.images:
        issues += _image_issues(image)
    return tuple(dict.fromkeys(issues))[: limits.MAX_ISSUES]


def blockers(issues: Iterable[CompletenessIssue]) -> tuple[CompletenessIssue, ...]:
    return tuple(issue for issue in issues if issue.level == "blocker")


def warnings(issues: Iterable[CompletenessIssue]) -> tuple[CompletenessIssue, ...]:
    return tuple(issue for issue in issues if issue.level == "warning")


__all__ = [
    "CARRIED",
    "CHECKED_FIELDS",
    "STICKY",
    "assess",
    "blockers",
    "leftover_keys",
    "warnings",
]
