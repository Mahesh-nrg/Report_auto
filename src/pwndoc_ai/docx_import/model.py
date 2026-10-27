"""Data model of the DOCX importer: frozen dataclasses, limits and strict JSON validation.

Everything here is data. Every ``from_json`` treats its input as hostile (a browser request or
a file on disk that may have been tampered with): it accepts only the documented keys, checks
types, lengths, patterns and cross references, and raises :class:`ImportInputError` naming the
schema path of the first bad value. ``to_json`` returns plain ``dict``/``list``/``str`` values
that ``from_json`` accepts again unchanged (a round trip is the identity).

HTML fields (``description``, ``observation``, ``remediation`` and ``html`` PoC blocks) are only
checked for type and length here; ``docx_import.html.clean_html`` enforces the tag allow-list.
See docs/IMPORTER_DESIGN.md for the contract.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import cvss
from . import jsoncheck as jc
from .limits import (
    CVSS_SCORE,
    CVSS_VECTOR,
    EMAIL,
    FINDING_FIELDS,
    FINDING_FLAGS,
    FINDING_KEY,
    FINDING_ORIGINS,
    HEADER_FIELDS,
    IMAGE_EDIT_FIELDS,
    IMAGE_FLAGS,
    IMAGE_KEY,
    IMAGE_TYPES,
    IMPORT_ID,
    ISSUE_FIELDS,
    ISSUES,
    LLM_FIELDS,
    LOCALE,
    MAX_CANDIDATES,
    MAX_CAPTION_CHARS,
    MAX_EMAIL_CHARS,
    MAX_FILE_NAME_CHARS,
    MAX_FINDINGS,
    MAX_IMAGE_PIXELS,
    MAX_IMAGES,
    MAX_ISSUES,
    MAX_LINE_CHARS,
    MAX_NAME_CHARS,
    MAX_POC_BLOCKS,
    MAX_POC_CHARS,
    MAX_REFERENCE_CHARS,
    MAX_REFERENCES,
    MAX_REVISION,
    MAX_RICH_CHARS,
    MAX_SCOPE_CHARS,
    MAX_SCOPE_ITEMS,
    MAX_TITLE_CHARS,
    MAX_TOTAL_IMAGE_BYTES,
    MAX_URLS,
    OBJECT_ID,
    SEVERITIES,
    SHA256,
    SOURCE_ID,
    UNUSABLE_IMAGE_FLAGS,
)
from .records import (
    AnalyseRequest,
    AuditTypeOption,
    Choice,
    CreatedObject,
    CreateRequest,
    ImportResult,
    ImportStatus,
    Journal,
    NameCheck,
    PlanDefaults,
    PwnDocOptions,
    UndoItem,
    UndoResult,
)


@dataclass(frozen=True)
class Contact:
    """A client contact. Only created in PwnDoc when ``email`` is set."""

    email: str
    firstname: str = ""
    lastname: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"email": self.email, "firstname": self.firstname, "lastname": self.lastname}

    @classmethod
    def from_json(cls, value: Any, path: str = "contact") -> Contact:
        data = jc.record(value, path, ("email", "firstname", "lastname"))
        email = jc.text(data["email"], f"{path}.email", MAX_EMAIL_CHARS, pattern=EMAIL)
        if not email:
            raise jc.fail(f"{path}.email")
        return cls(
            email=email,
            firstname=jc.text(data["firstname"], f"{path}.firstname", MAX_NAME_CHARS),
            lastname=jc.text(data["lastname"], f"{path}.lastname", MAX_NAME_CHARS),
        )


@dataclass(frozen=True)
class HeaderFields:
    """What the reader found on the cover, header and front tables. ``company`` is the majority
    vote of the reliable sources (never body prose); ``company_candidates`` every distinct name
    seen there; ``leftover_names`` known template names found in the body text."""

    company: str = ""
    company_votes: int = 0
    company_candidates: tuple[str, ...] = ()
    scope: tuple[str, ...] = ()
    date_start: str = ""  # ISO YYYY-MM-DD or ""
    date_end: str = ""
    test_type: str = ""
    leftover_names: tuple[str, ...] = ()
    contacts: tuple[Contact, ...] = ()
    severity_table: tuple[tuple[str, int], ...] = ()  # (severity, count) as printed

    def to_json(self) -> dict[str, Any]:
        return {
            "company": self.company,
            "company_votes": self.company_votes,
            "company_candidates": list(self.company_candidates),
            "scope": list(self.scope),
            "date_start": self.date_start,
            "date_end": self.date_end,
            "test_type": self.test_type,
            "leftover_names": list(self.leftover_names),
            "contacts": [c.to_json() for c in self.contacts],
            "severity_table": [[name, count] for name, count in self.severity_table],
        }

    @classmethod
    def from_json(cls, value: Any, path: str = "source_header") -> HeaderFields:
        data = jc.record(value, path, cls.__dataclass_fields__)
        return cls(
            company=jc.text(data["company"], f"{path}.company", MAX_NAME_CHARS),
            company_votes=jc.count(data["company_votes"], f"{path}.company_votes", 100),
            company_candidates=jc.lines(
                data["company_candidates"],
                f"{path}.company_candidates",
                MAX_CANDIDATES,
                MAX_NAME_CHARS,
            ),  # fmt: skip
            scope=jc.lines(data["scope"], f"{path}.scope", MAX_SCOPE_ITEMS, MAX_SCOPE_CHARS),
            date_start=jc.iso_date(data["date_start"], f"{path}.date_start"),
            date_end=jc.iso_date(data["date_end"], f"{path}.date_end"),
            test_type=jc.text(data["test_type"], f"{path}.test_type", MAX_NAME_CHARS),
            leftover_names=jc.lines(
                data["leftover_names"], f"{path}.leftover_names", MAX_CANDIDATES, MAX_NAME_CHARS
            ),
            contacts=jc.items(
                data["contacts"], f"{path}.contacts", MAX_CANDIDATES, Contact.from_json
            ),
            severity_table=jc.items(
                data["severity_table"], f"{path}.severity_table", len(SEVERITIES), _severity_count
            ),
        )


def _severity_count(value: Any, path: str) -> tuple[str, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise jc.fail(path)
    return jc.choice(value[0], f"{path}[0]", SEVERITIES), jc.count(value[1], f"{path}[1]")


@dataclass(frozen=True)
class PocBlock:
    """One piece of a finding's PoC: allow-list HTML (no ``<img>``) or a screenshot reference.
    The writer turns an image block into ``<img src="<pwndoc id>" alt="<caption>">`` placed
    between complete block elements."""

    kind: str  # "html" or "image"
    html: str = ""
    image_key: str = ""

    def to_json(self) -> dict[str, Any]:
        if self.kind == "image":
            return {"kind": "image", "image_key": self.image_key}
        return {"kind": "html", "html": self.html}

    @classmethod
    def from_json(cls, value: Any, path: str = "poc") -> PocBlock:
        if not isinstance(value, dict) or value.get("kind") not in ("html", "image"):
            raise jc.fail(path)
        if value["kind"] == "image":
            data = jc.record(value, path, ("kind", "image_key"))
            key = jc.text(data["image_key"], f"{path}.image_key", 5, pattern=IMAGE_KEY)
            if not key:
                raise jc.fail(f"{path}.image_key")
            return cls(kind="image", image_key=key)
        data = jc.record(value, path, ("kind", "html"))
        html = jc.text(data["html"], f"{path}.html", MAX_RICH_CHARS, line=False)
        if not html.strip():
            raise jc.fail(f"{path}.html")
        return cls(kind="html", html=html)


def _poc(value: Any, path: str) -> tuple[PocBlock, ...]:
    blocks = jc.items(value, path, MAX_POC_BLOCKS, PocBlock.from_json)
    if sum(len(block.html) for block in blocks) > MAX_POC_CHARS:
        raise jc.fail(path)
    return blocks


def _finding_values(data: dict, path: str) -> dict[str, Any]:
    """Parse whichever editable finding fields ``data`` holds (shared by findings and edits)."""
    parsers: dict[str, Callable[[Any, str], Any]] = {
        "title": lambda v, p: jc.text(v, p, MAX_TITLE_CHARS),
        "severity": lambda v, p: "" if v == "" else jc.choice(v, p, SEVERITIES),
        "cvss_vector": lambda v, p: jc.text(v, p, 200, pattern=CVSS_VECTOR),
        "cvss_score": lambda v, p: jc.text(v, p, 4, pattern=CVSS_SCORE),
        "cwe": lambda v, p: jc.text(v, p, MAX_LINE_CHARS),
        "urls": lambda v, p: jc.lines(v, p, MAX_URLS, MAX_LINE_CHARS),
        "parameter": lambda v, p: jc.text(v, p, MAX_LINE_CHARS),
        "location": lambda v, p: jc.text(v, p, MAX_LINE_CHARS),
        "description": lambda v, p: jc.text(v, p, MAX_RICH_CHARS, line=False),
        "observation": lambda v, p: jc.text(v, p, MAX_RICH_CHARS, line=False),
        "poc": _poc,
        "remediation": lambda v, p: jc.text(v, p, MAX_RICH_CHARS, line=False),
        "references": lambda v, p: jc.lines(v, p, MAX_REFERENCES, MAX_REFERENCE_CHARS),
        "include": jc.flag,
    }
    return {
        name: parsers[name](data[name], f"{path}.{name}") for name in FINDING_FIELDS if name in data
    }


@dataclass(frozen=True)
class ImportedFinding:
    """One deduplicated finding. ``key`` (``f001``...) is local and stable for the import;
    ``source_id`` is the number printed in the document (``"003"``), kept as metadata only."""

    key: str
    title: str
    source_id: str = ""
    severity: str = ""  # one of SEVERITIES or ""
    cvss_vector: str = ""  # "CVSS:3.1/AV:..." or ""; the document normally has only a score
    cvss_score: str = ""  # "8.8" as printed, or ""
    cwe: str = ""
    urls: tuple[str, ...] = ()
    parameter: str = ""
    location: str = ""
    description: str = ""  # allow-list HTML
    observation: str = ""  # allow-list HTML
    poc: tuple[PocBlock, ...] = ()
    remediation: str = ""  # allow-list HTML
    references: tuple[str, ...] = ()
    include: bool = True
    origin: str = "detail"  # FINDING_ORIGINS
    llm_fields: tuple[str, ...] = ()  # fields the LLM fallback filled
    flags: tuple[str, ...] = ()  # FINDING_FLAGS

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "source_id": self.source_id,
            "title": self.title,
            "severity": self.severity,
            "cvss_vector": self.cvss_vector,
            "cvss_score": self.cvss_score,
            "cwe": self.cwe,
            "urls": list(self.urls),
            "parameter": self.parameter,
            "location": self.location,
            "description": self.description,
            "observation": self.observation,
            "poc": [block.to_json() for block in self.poc],
            "remediation": self.remediation,
            "references": list(self.references),
            "include": self.include,
            "origin": self.origin,
            "llm_fields": list(self.llm_fields),
            "flags": list(self.flags),
        }

    @classmethod
    def from_json(cls, value: Any, path: str = "finding") -> ImportedFinding:
        data = jc.record(value, path, cls.__dataclass_fields__)
        key = jc.text(data["key"], f"{path}.key", 5, pattern=FINDING_KEY)
        if not key:
            raise jc.fail(f"{path}.key")
        return cls(
            key=key,
            source_id=jc.text(data["source_id"], f"{path}.source_id", 6, pattern=SOURCE_ID),
            origin=jc.choice(data["origin"], f"{path}.origin", FINDING_ORIGINS),
            llm_fields=jc.flags(data["llm_fields"], f"{path}.llm_fields", LLM_FIELDS),
            flags=jc.flags(data["flags"], f"{path}.flags", FINDING_FLAGS),
            **_finding_values(data, path),
        )


@dataclass(frozen=True)
class ImportedImage:
    """One extracted picture. ``finding_key`` is where it goes now (editable), ``""`` when the
    user detached it; ``source_finding_key`` where the reader found it. An image can only be
    ``include``-d when its type is in IMAGE_TYPES and it has no UNUSABLE_IMAGE_FLAGS."""

    key: str
    sha256: str
    media_type: str  # one of IMAGE_TYPES, or "" when unsupported
    size: int
    width: int = 0
    height: int = 0
    caption: str = ""  # without the "Figure N:" prefix; becomes the img alt
    finding_key: str = ""
    source_finding_key: str = ""
    include: bool = True
    order: int = 0  # position in the document, for "nearest finding" placement
    flags: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.media_type in IMAGE_TYPES and not UNUSABLE_IMAGE_FLAGS & set(self.flags)

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "size": self.size,
            "width": self.width,
            "height": self.height,
            "caption": self.caption,
            "finding_key": self.finding_key,
            "source_finding_key": self.source_finding_key,
            "include": self.include,
            "order": self.order,
            "flags": list(self.flags),
        }

    @classmethod
    def from_json(cls, value: Any, path: str = "image") -> ImportedImage:
        data = jc.record(value, path, cls.__dataclass_fields__)
        key = jc.text(data["key"], f"{path}.key", 5, pattern=IMAGE_KEY)
        digest = jc.text(data["sha256"], f"{path}.sha256", 64, pattern=SHA256)
        if not key or not digest:
            raise jc.fail(path)
        image = cls(
            key=key,
            sha256=digest,
            media_type=""
            if data["media_type"] == ""
            else jc.choice(data["media_type"], f"{path}.media_type", IMAGE_TYPES),  # fmt: skip
            size=jc.count(data["size"], f"{path}.size", MAX_TOTAL_IMAGE_BYTES),
            width=jc.count(data["width"], f"{path}.width", MAX_IMAGE_PIXELS),
            height=jc.count(data["height"], f"{path}.height", MAX_IMAGE_PIXELS),
            caption=jc.text(data["caption"], f"{path}.caption", MAX_CAPTION_CHARS),
            finding_key=jc.text(data["finding_key"], f"{path}.finding_key", 5, pattern=FINDING_KEY),
            source_finding_key=jc.text(
                data["source_finding_key"], f"{path}.source_finding_key", 5, pattern=FINDING_KEY
            ),
            include=jc.flag(data["include"], f"{path}.include"),
            order=jc.count(data["order"], f"{path}.order"),
            flags=jc.flags(data["flags"], f"{path}.flags", IMAGE_FLAGS),
        )
        if image.include and not image.usable:
            raise jc.fail(f"{path}.include")
        return image


@dataclass(frozen=True)
class ImageBlob:
    """The bytes of one extracted image (never serialised to JSON; stored as a file)."""

    key: str
    data: bytes


@dataclass(frozen=True)
class CompletenessIssue:
    """One line of the completeness panel. ``level`` and ``message`` follow from ``code``."""

    code: str
    finding_key: str = ""
    image_key: str = ""
    fields: tuple[str, ...] = ()

    @property
    def level(self) -> str:
        return ISSUES[self.code][0]

    @property
    def message(self) -> str:
        return ISSUES[self.code][1]

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "level": self.level,
            "message": self.message,
            "finding_key": self.finding_key,
            "image_key": self.image_key,
            "fields": list(self.fields),
        }

    @classmethod
    def from_json(cls, value: Any, path: str = "issue") -> CompletenessIssue:
        data = jc.record(
            value, path, ("code", "level", "message", "finding_key", "image_key", "fields")
        )
        code = jc.choice(data["code"], f"{path}.code", ISSUES)
        if data["level"] != ISSUES[code][0] or data["message"] != ISSUES[code][1]:
            raise jc.fail(path)
        return cls(
            code=code,
            finding_key=jc.text(data["finding_key"], f"{path}.finding_key", 5, pattern=FINDING_KEY),
            image_key=jc.text(data["image_key"], f"{path}.image_key", 5, pattern=IMAGE_KEY),
            fields=jc.flags(data["fields"], f"{path}.fields", ISSUE_FIELDS),
        )


@dataclass(frozen=True)
class SourceStats:
    """Counts the reader saw, for the completeness panel and the run log (no text)."""

    summary_rows: int = 0
    detail_tables: int = 0
    merged: int = 0
    figures_deduped: int = 0
    source_ids: tuple[str, ...] = ()
    id_gaps: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "summary_rows": self.summary_rows,
            "detail_tables": self.detail_tables,
            "merged": self.merged,
            "figures_deduped": self.figures_deduped,
            "source_ids": list(self.source_ids),
            "id_gaps": list(self.id_gaps),
        }

    @classmethod
    def from_json(cls, value: Any, path: str = "stats") -> SourceStats:
        data = jc.record(value, path, cls.__dataclass_fields__)

        def ids(name: str) -> tuple[str, ...]:
            where = f"{path}.{name}"
            return jc.items(data[name], where, 2 * MAX_FINDINGS, _source_id)

        return cls(
            summary_rows=jc.count(data["summary_rows"], f"{path}.summary_rows"),
            detail_tables=jc.count(data["detail_tables"], f"{path}.detail_tables"),
            merged=jc.count(data["merged"], f"{path}.merged"),
            figures_deduped=jc.count(data["figures_deduped"], f"{path}.figures_deduped"),
            source_ids=ids("source_ids"),
            id_gaps=ids("id_gaps"),
        )


def _source_id(value: Any, path: str) -> str:
    text = jc.text(value, path, 6, pattern=SOURCE_ID)
    if not text:
        raise jc.fail(path)
    return text


@dataclass(frozen=True)
class ImportedReport:
    """Everything the deterministic reader extracted (internal; not sent to the browser).
    ``header_text`` is a bounded excerpt of the cover/header/front tables for the header LLM
    fallback only; it never contains finding text."""

    header: HeaderFields
    findings: tuple[ImportedFinding, ...]
    images: tuple[ImportedImage, ...] = ()
    blobs: tuple[ImageBlob, ...] = ()
    issues: tuple[CompletenessIssue, ...] = ()
    stats: SourceStats = SourceStats()
    header_text: str = ""


def _header_values(data: dict, path: str) -> dict[str, Any]:
    parsers: dict[str, Callable[[Any, str], Any]] = {
        "company": lambda v, p: jc.text(v, p, MAX_NAME_CHARS),
        "audit_name": lambda v, p: jc.text(v, p, MAX_NAME_CHARS),
        "language": lambda v, p: jc.text(v, p, 32, pattern=LOCALE),
        "audit_type": lambda v, p: jc.text(v, p, MAX_NAME_CHARS),
        "template_id": lambda v, p: jc.text(v, p, 24, pattern=OBJECT_ID),
        "date_start": jc.iso_date,
        "date_end": jc.iso_date,
        "scope": lambda v, p: jc.lines(v, p, MAX_SCOPE_ITEMS, MAX_SCOPE_CHARS),
        "contact": lambda v, p: jc.optional(v, Contact.from_json, p),
    }
    return {
        name: parsers[name](data[name], f"{path}.{name}") for name in HEADER_FIELDS if name in data
    }


@dataclass(frozen=True)
class AuditHeader:
    """The audit-level values the user confirms in the preview."""

    company: str = ""
    audit_name: str = ""
    language: str = ""  # PwnDoc locale code
    audit_type: str = ""  # PwnDoc audit type name
    template_id: str = ""
    date_start: str = ""
    date_end: str = ""
    scope: tuple[str, ...] = ()
    contact: Contact | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "company": self.company,
            "audit_name": self.audit_name,
            "language": self.language,
            "audit_type": self.audit_type,
            "template_id": self.template_id,
            "date_start": self.date_start,
            "date_end": self.date_end,
            "scope": list(self.scope),
            "contact": None if self.contact is None else self.contact.to_json(),
        }

    @classmethod
    def from_json(cls, value: Any, path: str = "header") -> AuditHeader:
        return cls(**_header_values(jc.record(value, path, HEADER_FIELDS), path))


@dataclass(frozen=True)
class ImportPlan:
    """The editable preview, persisted server-side. ``revision`` grows by one on every saved
    edit; a create or an edit naming an older revision is refused (409 "stale")."""

    import_id: str
    revision: int
    file_name: str
    file_sha256: str
    header: AuditHeader
    source_header: HeaderFields
    findings: tuple[ImportedFinding, ...]
    images: tuple[ImportedImage, ...] = ()
    issues: tuple[CompletenessIssue, ...] = ()
    stats: SourceStats = SourceStats()
    options: PwnDocOptions = PwnDocOptions()
    company_id: str = ""  # existing PwnDoc company matched by name, "" = will be created
    duplicate_audit: bool = False
    previous_import: str = ""  # id of an earlier created import of the same file, or ""

    def to_json(self) -> dict[str, Any]:
        return {
            "import_id": self.import_id,
            "revision": self.revision,
            "file_name": self.file_name,
            "file_sha256": self.file_sha256,
            "header": self.header.to_json(),
            "source_header": self.source_header.to_json(),
            "findings": [f.to_json() for f in self.findings],
            "images": [i.to_json() for i in self.images],
            "issues": [i.to_json() for i in self.issues],
            "stats": self.stats.to_json(),
            "options": self.options.to_json(),
            "company_id": self.company_id,
            "duplicate_audit": self.duplicate_audit,
            "previous_import": self.previous_import,
        }

    @classmethod
    def from_json(cls, value: Any, path: str = "plan") -> ImportPlan:
        data = jc.record(value, path, cls.__dataclass_fields__)
        plan = cls(
            import_id=jc.required(data["import_id"], f"{path}.import_id", 32, IMPORT_ID),
            revision=jc.count(data["revision"], f"{path}.revision", MAX_REVISION),
            file_name=jc.text(data["file_name"], f"{path}.file_name", MAX_FILE_NAME_CHARS),
            file_sha256=jc.required(data["file_sha256"], f"{path}.file_sha256", 64, SHA256),
            header=AuditHeader.from_json(data["header"], f"{path}.header"),
            source_header=HeaderFields.from_json(data["source_header"], f"{path}.source_header"),
            findings=jc.items(
                data["findings"], f"{path}.findings", MAX_FINDINGS, ImportedFinding.from_json
            ),
            images=jc.items(data["images"], f"{path}.images", MAX_IMAGES, ImportedImage.from_json),
            issues=jc.items(
                data["issues"], f"{path}.issues", MAX_ISSUES, CompletenessIssue.from_json
            ),
            stats=SourceStats.from_json(data["stats"], f"{path}.stats"),
            options=PwnDocOptions.from_json(data["options"], f"{path}.options"),
            company_id=jc.text(data["company_id"], f"{path}.company_id", 24, pattern=OBJECT_ID),
            duplicate_audit=jc.flag(data["duplicate_audit"], f"{path}.duplicate_audit"),
            previous_import=jc.text(
                data["previous_import"], f"{path}.previous_import", 32, pattern=IMPORT_ID
            ),
        )
        check_references(plan, path)
        return plan


def check_references(plan: ImportPlan, path: str = "plan") -> None:
    """Unique keys; every finding/image key an image, PoC block or issue names exists."""
    findings = [f.key for f in plan.findings]
    images = [i.key for i in plan.images]
    jc.unique(findings, f"{path}.findings")
    jc.unique(images, f"{path}.images")
    known_f, known_i = set(findings) | {""}, set(images) | {""}
    for index, image in enumerate(plan.images):
        if image.finding_key not in known_f or image.source_finding_key not in known_f:
            raise jc.fail(f"{path}.images[{index}].finding_key")
    for index, finding in enumerate(plan.findings):
        if any(b.kind == "image" and b.image_key not in known_i for b in finding.poc):
            raise jc.fail(f"{path}.findings[{index}].poc")
    for index, issue in enumerate(plan.issues):
        if issue.finding_key not in known_f or issue.image_key not in known_i:
            raise jc.fail(f"{path}.issues[{index}]")


@dataclass(frozen=True)
class FieldEdit:
    """A partial change: ``values`` holds only the fields the request named."""

    key: str
    values: tuple[tuple[str, Any], ...]

    def get(self, name: str, default: Any = None) -> Any:
        return dict(self.values).get(name, default)

    @property
    def present(self) -> frozenset[str]:
        return frozenset(name for name, _ in self.values)


def _edited_vector(value: str, path: str) -> str:
    """An edited CVSS vector must be a complete, valid v3 base vector; stored canonical."""
    if not value:
        return value
    parsed = cvss.parse_vector(value)
    if parsed is None or not CVSS_VECTOR.fullmatch(parsed.vector):
        raise jc.fail(path)
    return parsed.vector


def _finding_edit(value: Any, path: str) -> FieldEdit:
    data = jc.record(value, path, ("key",), FINDING_FIELDS)
    key = jc.required(data["key"], f"{path}.key", 5, FINDING_KEY)
    values = _finding_values(data, path)
    if "cvss_vector" in values:
        values["cvss_vector"] = _edited_vector(values["cvss_vector"], f"{path}.cvss_vector")
    return FieldEdit(key=key, values=tuple(values.items()))


def _image_edit(value: Any, path: str) -> FieldEdit:
    data = jc.record(value, path, ("key",), IMAGE_EDIT_FIELDS)
    key = jc.required(data["key"], f"{path}.key", 5, IMAGE_KEY)
    parsers: dict[str, Callable[[Any, str], Any]] = {
        "caption": lambda v, p: jc.text(v, p, MAX_CAPTION_CHARS),
        "finding_key": lambda v, p: jc.text(v, p, 5, pattern=FINDING_KEY),
        "include": jc.flag,
    }
    values = tuple(
        (name, parsers[name](data[name], f"{path}.{name}"))
        for name in IMAGE_EDIT_FIELDS
        if name in data
    )
    return FieldEdit(key=key, values=values)


@dataclass(frozen=True)
class PlanEdits:
    """Body of ``PUT .../preview``. Semantic checks (keys exist, image usable) are done by
    ``plan.apply_edits``, which raises ImportInputError("unknown_key" / "image_not_usable")."""

    revision: int
    header: FieldEdit | None = None  # key == "header"
    findings: tuple[FieldEdit, ...] = ()
    images: tuple[FieldEdit, ...] = ()

    @classmethod
    def from_json(cls, value: Any, path: str = "edits") -> PlanEdits:
        data = jc.record(value, path, ("revision",), ("header", "findings", "images"))
        header = None
        if "header" in data:
            raw = jc.record(data["header"], f"{path}.header", (), HEADER_FIELDS)
            header = FieldEdit("header", tuple(_header_values(raw, f"{path}.header").items()))
        findings = jc.items(
            data.get("findings", []), f"{path}.findings", MAX_FINDINGS, _finding_edit
        )
        images = jc.items(data.get("images", []), f"{path}.images", MAX_IMAGES, _image_edit)
        jc.unique((e.key for e in findings), f"{path}.findings")
        jc.unique((e.key for e in images), f"{path}.images")
        return cls(
            revision=jc.count(data["revision"], f"{path}.revision", MAX_REVISION),
            header=header,
            findings=findings,
            images=images,
        )


__all__ = [
    "AnalyseRequest",
    "AuditHeader",
    "AuditTypeOption",
    "Choice",
    "CompletenessIssue",
    "Contact",
    "CreateRequest",
    "CreatedObject",
    "FieldEdit",
    "HeaderFields",
    "ImageBlob",
    "ImportPlan",
    "ImportResult",
    "ImportStatus",
    "ImportedFinding",
    "ImportedImage",
    "ImportedReport",
    "NameCheck",
    "PlanDefaults",
    "Journal",
    "PlanEdits",
    "PocBlock",
    "PwnDocOptions",
    "SourceStats",
    "UndoItem",
    "UndoResult",
    "check_references",
]
