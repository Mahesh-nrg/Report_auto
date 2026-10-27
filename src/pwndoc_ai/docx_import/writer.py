"""Create one confirmed import plan in PwnDoc, with the user's own session (contract §d).

Sequence: checks, company (reused by name or created), contact, audit, ``PUT /general``
(company by ``_id``, template, scope list, ISO dates), screenshots (one upload per distinct
sha256, journaled only when the row belongs to the new audit), findings one at a time in
``creation_order``, then a verification read that journals the finding ids. Every created id is
journaled the moment PwnDoc returns it. Any failure after the first write, a cancel included,
rolls back exactly the journaled ids with ``undo.undo_import``. Nothing here raises for PwnDoc
or cancel problems: the outcome carries the error and the rollback.
"""

from __future__ import annotations

import hashlib
import html as _html
import logging
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .. import cvss
from ..pwndoc_client import PwnDocClient, PwnDocError
from . import html as import_html
from . import images as import_images
from . import limits, lookups, ordering, undo
from .errors import (
    ImportCancelled,
    ImportConflict,
    ImportDenied,
    ImportFailure,
    ImportInputError,
    ImportSessionExpired,
    ImportUpstreamError,
)
from .journal import JournalWriter
from .model import ImportedFinding, ImportedImage, ImportPlan
from .normalize import normalize_findings
from .records import ImportResult, NameCheck, UndoResult
from .undo import OpenClient, Progress

log = logging.getLogger("pwndoc_ai.docx_import")

STEP_CHECK = "Checking PwnDoc"
STEP_COMPANY = "Creating the company"
STEP_CONTACT = "Creating the contact"
STEP_AUDIT = "Creating the audit"
STEP_GENERAL = "Setting audit details"
STEP_IMAGES = "Uploading screenshots"
STEP_FINDINGS = "Creating findings"
STEP_VERIFY = "Verifying"
FIXED_STEPS = 6  # check, company, contact, audit, general, verify

FINDING_STATUS = 1  # PwnDoc: 0 = done, 1 = redacting (the user reviews before publishing)
CUSTOM_DISPLAYS = ("finding", "vulnerability")
CHOICE_FIELD_TYPES = ("select", "radio")
IMAGE_EXTS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif"}
NO_SOURCE_ID = 10**7


@dataclass(frozen=True)
class CreateOutcome:
    result: ImportResult | None = None
    error: ImportFailure | None = None
    rollback: UndoResult | None = None


# -- ordering and HTML -------------------------------------------------------------------------
def score_of(finding: ImportedFinding) -> float | None:
    """The CVSS v3.1 base score (vector first, then the printed number), else ``None``."""
    return ordering.finding_score(finding)


def creation_order(findings: Sequence[ImportedFinding]) -> tuple[ImportedFinding, ...]:
    """Included findings in docs/FINDING_ORDER.md Part A order: band of the v3.1 score
    (Critical first), score descending, unscored after the scored ones of their label's band,
    ties in plan order. PwnDoc numbers them last+1, so this order is the new S. No.
    Each finding is normalised first (Part B: estimated vector, derived band, CWE, N/A fills)."""
    return tuple(f for f in normalize_findings(findings) if f.include)


def _placeable(
    finding: ImportedFinding, images: Sequence[ImportedImage], image_ids: Mapping[str, str]
) -> dict[str, ImportedImage]:
    return {
        image.key: image
        for image in images
        if image.include
        and image.usable
        and image.finding_key == finding.key
        and limits.OBJECT_ID.fullmatch(image_ids.get(image.key, ""))
    }


def poc_html(
    finding: ImportedFinding, images: Sequence[ImportedImage], image_ids: Mapping[str, str]
) -> str:
    """The PoC blocks joined; image blocks become top-level ``<img>`` tags between complete
    blocks, and attached images without a block are appended in document order."""
    placeable = _placeable(finding, images, image_ids)
    parts: list[str] = []
    placed: set[str] = set()
    for block in finding.poc:
        if block.kind == "html":
            parts.append(block.html)
        elif block.image_key in placeable and block.image_key not in placed:
            image = placeable[block.image_key]
            parts.append(import_html.image_tag(image_ids[image.key], image.caption))
            placed.add(image.key)
    for image in sorted(placeable.values(), key=lambda i: (i.order, i.key)):
        if image.key not in placed:
            parts.append(import_html.image_tag(image_ids[image.key], image.caption))
    return "".join(parts)


# -- finding payload ---------------------------------------------------------------------------
def field_key(label: str) -> str:
    """PwnDoc's template key for a custom field label:
    ``deburr(lower(label)).replace(/\\s/g, "").replace(/[^\\w]/g, "_")``."""
    text = unicodedata.normalize("NFKD", label)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"\s", "", text)
    return re.sub(r"[^\w]", "_", text, flags=re.ASCII)


def _severity_text(finding: ImportedFinding) -> str:
    """The band of the v3.1 score replaces any conflicting label; no score keeps the label."""
    severity = ordering.derived_severity(finding)
    return severity if severity in limits.SEVERITIES else ""


def custom_values(finding: ImportedFinding) -> dict[str, str]:
    """Template key -> value for the custom fields the active template prints."""
    computed = score_of(finding)
    score = f"{computed:.1f}" if computed is not None else ""
    return {
        "vulnerableurl": ", ".join(finding.urls),
        "vulnerableparameter": finding.parameter,
        "vulnerablelocation": finding.location,
        "cwecve": finding.cwe,
        "cwe_cve": finding.cwe,
        "cwe": finding.cwe,
        "cvssscore": score,
        "severity": _severity_text(finding),
    }


def finding_fields(raw: Any) -> tuple[dict[str, Any], ...]:
    """The custom fields shown on findings (``display`` finding/vulnerability, no sub-display)."""
    fields = raw if isinstance(raw, list) else []
    return tuple(
        f
        for f in fields
        if isinstance(f, dict)
        and f.get("display") in CUSTOM_DISPLAYS
        and not f.get("displaySub")
        and isinstance(f.get("label"), str)
        and lookups.ref_id(f.get("_id"))
    )


def _choice_value(field: dict[str, Any], value: str, language: str) -> str:
    """For a select/radio field, the option of ``language`` matching ``value``; ``""`` when the
    field has options in that language but none matches (an unlisted value would show blank)."""
    if field.get("fieldType") not in CHOICE_FIELD_TYPES:
        return value
    options = field.get("options") if isinstance(field.get("options"), list) else []
    mine = [
        o["value"]
        for o in options
        if isinstance(o, dict) and o.get("locale") == language and isinstance(o.get("value"), str)
    ]
    if not mine:
        return value
    return next((o for o in mine if o.casefold() == value.casefold()), "")


def _custom_fields(
    finding: ImportedFinding, custom_fields: Sequence[dict], language: str
) -> list[dict[str, Any]]:
    values = custom_values(finding)
    out = []
    for field in custom_fields:
        value = values.get(field_key(field.get("label", "")), "")
        value = _choice_value(field, value, language) if value else ""
        if value:
            definition = {k: v for k, v in field.items() if k != "text"}
            out.append({"customField": definition, "text": value})
    return out


def finding_payload(
    finding: ImportedFinding, *, poc: str, custom_fields: Sequence[dict], language: str
) -> dict[str, Any]:
    """The ``POST /api/audits/:id/findings`` body (never ``priority``/``remediationComplexity``)."""
    body: dict[str, Any] = {
        "title": finding.title,
        "description": finding.description,
        "observation": finding.observation,
        "remediation": finding.remediation,
        "poc": poc,
        "references": list(finding.references),
        "status": FINDING_STATUS,
    }
    parsed = cvss.parse_vector(finding.cvss_vector) if finding.cvss_vector else None
    if parsed is not None:
        body["cvssv3"] = parsed.vector
    if finding.urls:
        body["scope"] = "".join(f"<p>{_html.escape(url, quote=True)}</p>" for url in finding.urls)
    fields = _custom_fields(finding, custom_fields, language)
    if fields:
        body["customFields"] = fields
    return body


# -- the create run ----------------------------------------------------------------------------
class _Run:
    """One create attempt: the client, the journal and the progress counter."""

    def __init__(
        self,
        plan: ImportPlan,
        client: PwnDocClient,
        journal: JournalWriter,
        progress: Progress,
        cancelled: Callable[[], bool],
    ) -> None:
        self.plan = plan
        self.client = client
        self.journal = journal
        self._progress = progress
        self._cancelled = cancelled
        self.findings = creation_order(plan.findings)
        self.images = eligible_images(plan)
        groups = {image.sha256 for image in self.images}
        self.total = FIXED_STEPS + len(groups) + len(self.findings)
        self.done = 0

    def step(self, label: str) -> None:
        if self._cancelled():
            raise ImportCancelled()
        self._progress(label, self.done, self.total)
        self.done += 1


def eligible_images(plan: ImportPlan) -> tuple[ImportedImage, ...]:
    """Included, usable images attached to an included finding, in document order."""
    keys = {f.key for f in plan.findings if f.include}
    chosen = (i for i in plan.images if i.include and i.usable and i.finding_key in keys)
    return tuple(sorted(chosen, key=lambda i: (i.order, i.key)))


def _check_plan(plan: ImportPlan) -> None:
    head = plan.header
    options = plan.options
    if not (head.company.strip() and head.audit_name.strip()):
        raise ImportInputError("blockers")
    if head.language not in {c.value for c in options.languages}:
        raise ImportInputError("blockers")
    if head.audit_type not in {a.name for a in options.audit_types}:
        raise ImportInputError("blockers")
    if head.template_id not in {c.value for c in options.templates}:
        raise ImportInputError("blockers")
    if not any(f.include and f.title.strip() for f in plan.findings):
        raise ImportInputError("blockers")
    if any(f.include and not f.title.strip() for f in plan.findings):
        raise ImportInputError("blockers")


def _checks(run: _Run, confirm_duplicate: bool) -> tuple[NameCheck, tuple[dict, ...]]:
    run.step(STEP_CHECK)
    _check_plan(run.plan)
    names = lookups.check_names(run.client, run.plan.header.company, run.plan.header.audit_name)
    if names.duplicate_audit and not confirm_duplicate:
        raise ImportConflict("duplicate_audit")
    return names, finding_fields(run.client.list_custom_fields())


def _ensure_company(run: _Run, names: NameCheck) -> tuple[str, bool]:
    run.step(STEP_COMPANY)
    if names.company_id:
        return names.company_id, False
    name = run.plan.header.company.strip()
    try:
        created = run.client.create_company(name)
    except PwnDocError as exc:
        if exc.status != 422:
            raise
        existing = lookups.find_company(run.client.list_companies(), name)
        if not existing:
            raise
        return existing, False  # created meanwhile by someone else: reuse, never journal
    company_id = lookups.ref_id(created.get("_id"))
    run.journal.record("company", company_id)
    return company_id, True


def _ensure_contact(run: _Run) -> tuple[str, bool]:
    run.step(STEP_CONTACT)
    contact = run.plan.header.contact
    if contact is None or not contact.email:
        return "", False
    wanted = contact.email.casefold()
    for row in run.client.list_clients() or []:
        email = row.get("email") if isinstance(row, dict) else None
        if isinstance(email, str) and email.casefold() == wanted and lookups.ref_id(row.get("_id")):
            return lookups.ref_id(row.get("_id")), False
    body = {"email": contact.email, "firstname": contact.firstname, "lastname": contact.lastname}
    client_id = lookups.ref_id(run.client.create_client(body).get("_id"))
    run.journal.record("client", client_id)
    return client_id, True


def _create_audit(run: _Run) -> str:
    run.step(STEP_AUDIT)
    head = run.plan.header
    audit_id = run.client.create_audit(head.audit_name.strip(), head.language, head.audit_type)
    run.journal.record("audit", audit_id)
    return audit_id


def general_payload(plan: ImportPlan, company_id: str, client_id: str) -> dict[str, Any]:
    """``PUT /general``: company by ``_id`` (never by name), scope as a list of strings, ISO
    dates only when set (PwnDoc ignores falsy values) and the template chosen explicitly."""
    head = plan.header
    payload: dict[str, Any] = {
        "company": {"_id": company_id},
        "template": head.template_id,
        "scope": list(head.scope),
    }
    if head.date_start:
        payload["date_start"] = head.date_start
    if head.date_end:
        payload["date_end"] = head.date_end
    if client_id:
        payload["client"] = client_id
    return payload


def _set_general(run: _Run, audit_id: str, company_id: str, client_id: str) -> None:
    run.step(STEP_GENERAL)
    run.client.update_audit_general(audit_id, general_payload(run.plan, company_id, client_id))


def _journaled(run: _Run, kind: str, object_id: str) -> bool:
    return any(o.kind == kind and o.id == object_id for o in run.journal.current.objects)


def _upload_one(run: _Run, image: ImportedImage, data: bytes, audit_id: str) -> tuple[str, bool]:
    """Upload one image; ``(id, new)``. ``new`` is False when PwnDoc reused a row by its bytes."""
    if hashlib.sha256(data).hexdigest() != image.sha256:
        raise ImportInputError("image_not_usable")
    name = f"{image.key}.{IMAGE_EXTS.get(image.media_type, 'png')}"
    image_id = run.client.create_image(import_images.data_url(data), name, audit_id)
    try:
        owner = lookups.ref_id(run.client.get_image(image_id).get("auditId"))
    except (PwnDocError, ValueError):
        # unknown owner: journal it so the rollback can clean up; undo re-reads the row and
        # keeps it when it belongs to another audit
        run.journal.record("image", image_id, audit_id)
        raise
    if owner != audit_id or _journaled(run, "image", image_id):
        return image_id, False
    run.journal.record("image", image_id, audit_id)
    return image_id, True


def _upload_images(
    run: _Run, audit_id: str, read_image: Callable[[str], bytes]
) -> tuple[dict[str, str], int, int]:
    groups: dict[str, list[ImportedImage]] = {}
    for image in run.images:
        groups.setdefault(image.sha256, []).append(image)
    ids: dict[str, str] = {}
    uploaded = reused = 0
    for group in groups.values():
        run.step(STEP_IMAGES)
        image_id, new = _upload_one(run, group[0], read_image(group[0].key), audit_id)
        uploaded, reused = (uploaded + 1, reused) if new else (uploaded, reused + 1)
        ids.update((image.key, image_id) for image in group)
    return ids, uploaded, reused


def _create_findings(
    run: _Run, audit_id: str, image_ids: Mapping[str, str], fields: Sequence[dict]
) -> None:
    for finding in run.findings:  # strictly sequential: PwnDoc numbers them in this order
        run.step(STEP_FINDINGS)
        poc = poc_html(finding, run.plan.images, image_ids)
        body = finding_payload(
            finding, poc=poc, custom_fields=fields, language=run.plan.header.language
        )
        run.client.create_finding(audit_id, body)


def _identifier(finding: dict[str, Any]) -> int:
    value = finding.get("identifier")
    return value if isinstance(value, int) and not isinstance(value, bool) else NO_SOURCE_ID


def _verify(run: _Run, audit_id: str) -> tuple[str, ...]:
    run.step(STEP_VERIFY)
    audit = run.client.get_audit(audit_id)
    rows = audit.get("findings") if isinstance(audit, dict) else None
    found = sorted((f for f in rows or [] if isinstance(f, dict)), key=_identifier)
    ids = tuple(lookups.ref_id(f.get("_id")) for f in found)
    for finding_id in ids:
        if finding_id:
            run.journal.record("finding", finding_id, audit_id)
    if len(ids) != len(run.findings) or not all(ids):
        raise ImportUpstreamError("pwndoc")
    return ids


def _create(run: _Run, read_image: Callable[[str], bytes], confirm_duplicate: bool) -> ImportResult:
    names, fields = _checks(run, confirm_duplicate)
    company_id, company_created = _ensure_company(run, names)
    client_id, client_created = _ensure_contact(run)
    audit_id = _create_audit(run)
    _set_general(run, audit_id, company_id, client_id)
    image_ids, uploaded, reused = _upload_images(run, audit_id, read_image)
    _create_findings(run, audit_id, image_ids, fields)
    finding_ids = _verify(run, audit_id)
    return ImportResult(
        audit_id=audit_id,
        company_id=company_id,
        company_created=company_created,
        client_id=client_id,
        client_created=client_created,
        finding_ids=finding_ids,
        images_uploaded=uploaded,
        images_reused=reused,
        images_skipped=len(run.plan.images) - len(run.images),
    )


def as_failure(exc: Exception) -> ImportFailure:
    """A user-safe failure for ``exc`` (PwnDoc 401/403 keep their meaning)."""
    if isinstance(exc, ImportFailure):
        return exc
    if isinstance(exc, PwnDocError):
        if exc.status == 401:
            return ImportSessionExpired()
        if exc.status == 403:
            return ImportDenied()
        return ImportUpstreamError("pwndoc")
    if isinstance(exc, ValueError):  # an id PwnDoc returned failed safe_id
        return ImportUpstreamError("pwndoc")
    return ImportFailure()


def _rollback(
    journal: JournalWriter, open_client: OpenClient, progress: Progress
) -> UndoResult | None:
    if not journal.current.objects:
        return None
    result = undo.undo_import(
        journal.current, open_client=open_client, progress=progress, check_changes=False
    )
    try:
        journal.set_state("rolled_back" if undo.fully_undone(result) else "partial")
    except (OSError, ImportFailure) as exc:
        log.error("import rollback: journal not updated (%s)", type(exc).__name__)
    return result


def create_import(
    plan: ImportPlan,
    *,
    open_client: OpenClient,
    read_image: Callable[[str], bytes],
    journal: JournalWriter,
    progress: Progress,
    cancelled: Callable[[], bool],
    confirm_duplicate: bool,
) -> CreateOutcome:
    """Create ``plan`` in PwnDoc; on any failure roll back what this attempt journaled."""
    try:
        with open_client() as client:
            run = _Run(plan, client, journal, progress, cancelled)
            result = _create(run, read_image, confirm_duplicate)
        journal.set_state("complete")
        progress(STEP_VERIFY, run.total, run.total)
        return CreateOutcome(result=result)
    except (ImportFailure, PwnDocError, ValueError, OSError) as exc:
        log.warning("import create failed (%s)", type(exc).__name__)
        return CreateOutcome(
            error=as_failure(exc), rollback=_rollback(journal, open_client, progress)
        )
    except Exception:
        log.error("import create crashed; rolling back what it created")
        _rollback(journal, open_client, progress)
        raise
