"""The editable import preview: build it from the reader's output and merge the UI's edits.

Every value that leaves this module has been validated twice: by the strict model parsers (the
edit body) and by a final ``ImportPlan.from_json(plan.to_json())`` round trip, which also enforces
every cap and ``check_references``. Rich HTML from the browser is re-cleaned through the reader's
allow-list (``html.clean_html``), imported at call time so this module never loads the XML stack.

The document title is never consulted: the audit name defaults to the file name and the company to
the reader's vote of the reliable header sources.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

from . import completeness, limits
from .errors import ImportConflict, ImportInputError
from .model import (
    AuditHeader,
    FieldEdit,
    ImportedFinding,
    ImportedImage,
    ImportedReport,
    ImportPlan,
    NameCheck,
    PlanDefaults,
    PlanEdits,
    PocBlock,
    PwnDocOptions,
)
from .normalize import plan_order

Cleaner = Callable[[str], str]
DEFAULT_AUDIT_NAME = "Imported report"
RICH_FIELDS = ("description", "observation", "remediation")
LINE_FIELDS = ("title", "cvss_vector", "cvss_score", "cwe", "parameter", "location")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁤﻿]")
_DOCX_SUFFIX = re.compile(r"\.docx$", re.IGNORECASE)


def _one_line(value: str) -> str:
    return " ".join(_CONTROL.sub(" ", value).split())


def _lines(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(line for line in (_one_line(v) for v in values) if line))


def default_audit_name(file_name: str) -> str:
    """The base name without ``.docx``, one clean line of at most MAX_NAME_CHARS characters."""
    base = re.split(r"[\\/]", file_name or "")[-1]
    stem = _one_line(_DOCX_SUFFIX.sub("", _one_line(base)))
    return stem[: limits.MAX_NAME_CHARS].strip() or DEFAULT_AUDIT_NAME


def _pick(default: str, choices: Sequence[str]) -> str:
    if not choices:
        return default
    return default if default in choices else choices[0]


def _pick_audit_type(options: PwnDocOptions, default: str, language: str) -> str:
    names = [a.name for a in options.audit_types]
    fitting = [a.name for a in options.audit_types if language in a.locales]
    if default in fitting or (default in names and not fitting):
        return default
    return _pick(default, fitting or names)


def _header(report: ImportedReport, file_name: str, options: PwnDocOptions, defaults: PlanDefaults):
    source = report.header
    language = _pick(defaults.language, [c.value for c in options.languages])
    return AuditHeader(
        company=_one_line(source.company)[: limits.MAX_NAME_CHARS],
        audit_name=default_audit_name(file_name),
        language=language,
        audit_type=_pick_audit_type(options, defaults.audit_type, language),
        template_id=_pick(defaults.template_id, [c.value for c in options.templates]),
        date_start=source.date_start,
        date_end=source.date_end,
        scope=_lines(source.scope)[: limits.MAX_SCOPE_ITEMS],
        contact=source.contacts[0] if source.contacts else None,
    )


def _validated(plan: ImportPlan) -> ImportPlan:
    """The strict round trip: caps, patterns and cross references, or ImportInputError."""
    return ImportPlan.from_json(plan.to_json())


def build_plan(
    report: ImportedReport,
    *,
    import_id: str,
    file_name: str,
    file_sha256: str,
    options: PwnDocOptions,
    defaults: PlanDefaults,
    names: NameCheck,
    previous_import: str = "",
) -> ImportPlan:
    """The first preview (revision 1) of an analysed report."""
    draft = ImportPlan(
        import_id=import_id,
        revision=1,
        file_name=file_name[: limits.MAX_FILE_NAME_CHARS],
        file_sha256=file_sha256,
        header=_header(report, file_name, options, defaults),
        source_header=report.header,
        findings=plan_order(report.findings),
        images=report.images,
        issues=report.issues,
        stats=report.stats,
        options=options,
        company_id=names.company_id,
        duplicate_audit=names.duplicate_audit,
        previous_import=previous_import,
    )
    return _validated(replace(draft, issues=completeness.assess(draft)))


# -- edits --------------------------------------------------------------------------------------


def _name_key(value: str) -> str:
    from . import labels

    return labels.name_key(value)


def _default_cleaner() -> Cleaner:
    from . import html as html_tools

    return html_tools.clean_html


def _checked_choice(value: str, choices: Sequence[str], field: str) -> str:
    if value and choices and value not in choices:
        raise ImportInputError(field=f"header.{field}")
    return value


def _header_values(edit: FieldEdit, options: PwnDocOptions) -> dict[str, Any]:
    values = dict(edit.values)
    for name in ("company", "audit_name", "audit_type"):
        if name in values:
            values[name] = _one_line(values[name])
    if "scope" in values:
        values["scope"] = _lines(values["scope"])
    checks = {
        "language": [c.value for c in options.languages],
        "audit_type": [a.name for a in options.audit_types],
        "template_id": [c.value for c in options.templates],
    }
    for name, choices in checks.items():
        if name in values:
            _checked_choice(values[name], choices, name)
    return values


def _apply_header(plan: ImportPlan, edit: FieldEdit | None) -> ImportPlan:
    if edit is None:
        return plan
    header = replace(plan.header, **_header_values(edit, plan.options))
    if _name_key(header.company) != _name_key(plan.header.company):
        plan = replace(plan, company_id="", duplicate_audit=False)
    return replace(plan, header=header)


def _image_values(edit: FieldEdit, image: ImportedImage, findings: set[str]) -> dict[str, Any]:
    values = dict(edit.values)
    if "caption" in values:
        values["caption"] = _one_line(values["caption"])
    if values.get("finding_key", "") not in findings | {""}:
        raise ImportInputError("unknown_key")
    if values.get("include") and not image.usable:
        raise ImportInputError("image_not_usable")
    return values


def _apply_images(plan: ImportPlan, edits: Sequence[FieldEdit]) -> ImportPlan:
    by_key = {edit.key: edit for edit in edits}
    if set(by_key) - {image.key for image in plan.images}:
        raise ImportInputError("unknown_key")
    findings = {finding.key for finding in plan.findings}
    images = tuple(
        replace(image, **_image_values(by_key[image.key], image, findings))
        if image.key in by_key
        else image
        for image in plan.images
    )
    return replace(plan, images=images)


def _clean_poc(blocks: Sequence[PocBlock], images: set[str], clean: Cleaner) -> tuple:
    out: list[PocBlock] = []
    for block in blocks:
        if block.kind == "image":
            if block.image_key not in images:
                raise ImportInputError("unknown_key")
            out.append(block)
            continue
        html = clean(block.html)
        if html.strip():
            out.append(PocBlock(kind="html", html=html))
    return tuple(out)


def _finding_values(edit: FieldEdit, images: set[str], clean: Cleaner) -> dict[str, Any]:
    values = dict(edit.values)
    for name in LINE_FIELDS:
        if name in values:
            values[name] = _one_line(values[name])
    for name in RICH_FIELDS:
        if name in values:
            values[name] = clean(values[name])
    for name in ("urls", "references"):
        if name in values:
            values[name] = _lines(values[name])
    if "poc" in values:
        values["poc"] = _clean_poc(values["poc"], images, clean)
    return values


def _apply_findings(plan: ImportPlan, edits: Sequence[FieldEdit], clean: Cleaner) -> ImportPlan:
    by_key = {edit.key: edit for edit in edits}
    if set(by_key) - {finding.key for finding in plan.findings}:
        raise ImportInputError("unknown_key")
    images = {image.key for image in plan.images}
    findings = tuple(
        replace(finding, **_finding_values(by_key[finding.key], images, clean))
        if finding.key in by_key
        else finding
        for finding in plan.findings
    )
    return replace(plan, findings=findings)


def _settle_image_blocks(plan: ImportPlan) -> ImportPlan:
    """A screenshot block stays only in the PoC of the finding the screenshot is attached to,
    at most once; moving or detaching a screenshot removes its blocks everywhere else."""
    owner = {image.key: image.finding_key for image in plan.images}

    def settle(finding: ImportedFinding) -> ImportedFinding:
        seen: set[str] = set()
        kept = []
        for block in finding.poc:
            if block.kind == "image":
                if owner.get(block.image_key) != finding.key or block.image_key in seen:
                    continue
                seen.add(block.image_key)
            kept.append(block)
        return finding if len(kept) == len(finding.poc) else replace(finding, poc=tuple(kept))

    return replace(plan, findings=tuple(settle(f) for f in plan.findings))


def apply_edits(
    plan: ImportPlan,
    edits: PlanEdits,
    *,
    names: NameCheck | None = None,
    clean: Cleaner | None = None,
) -> ImportPlan:
    """The plan with ``edits`` merged, issues and the finding order recomputed (never taken from
    the client) and ``revision`` + 1.

    ``clean`` defaults to ``html.clean_html``; tests may inject the same allow-list function.
    """
    if edits.revision != plan.revision:
        raise ImportConflict("stale")
    cleaner = clean or _default_cleaner()
    merged = _apply_header(plan, edits.header)
    merged = _apply_images(merged, edits.images)
    merged = _apply_findings(merged, edits.findings, cleaner)
    merged = _settle_image_blocks(merged)
    if names is not None:
        merged = replace(merged, company_id=names.company_id, duplicate_audit=names.duplicate_audit)
    merged = replace(merged, findings=plan_order(merged.findings))
    merged = replace(merged, revision=plan.revision + 1)
    return _validated(replace(merged, issues=completeness.assess(merged)))


__all__ = ["DEFAULT_AUDIT_NAME", "apply_edits", "build_plan", "default_audit_name"]
