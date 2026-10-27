"""Request bodies, the undo journal, results and status records of the DOCX importer.

Frozen dataclasses with strict ``from_json`` (hostile input) and ``to_json`` (round trip).
They hold PwnDoc ids and counts only, never names or document text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import jsoncheck as jc
from .limits import (
    IMPORT_ID,
    IMPORT_STATES,
    JOURNAL_STATES,
    LOCALE,
    MAX_CURRENT_CHARS,
    MAX_ERROR_CHARS,
    MAX_FILE_NAME_CHARS,
    MAX_FINDINGS,
    MAX_IMAGES,
    MAX_JOURNAL_OBJECTS,
    MAX_NAME_CHARS,
    MAX_OPTIONS,
    MAX_REVISION,
    OBJECT_ID,
    OBJECT_KINDS,
    PHASES,
    TIMESTAMP,
    UNDO_OUTCOMES,
)


@dataclass(frozen=True)
class AnalyseRequest:
    """Body of ``POST /__ai/api/docx/import/imports``."""

    file_id: str
    use_llm: bool = True

    @classmethod
    def from_json(cls, value: Any, path: str = "body") -> AnalyseRequest:
        data = jc.record(value, path, ("file_id",), ("use_llm",))
        return cls(
            file_id=jc.required(data["file_id"], f"{path}.file_id", 32, IMPORT_ID),
            use_llm=jc.flag(data.get("use_llm", True), f"{path}.use_llm"),
        )


@dataclass(frozen=True)
class CreateRequest:
    """Body of ``POST .../imports/{id}/create``."""

    revision: int
    accept_blanks: bool = False
    confirm_duplicate: bool = False

    @classmethod
    def from_json(cls, value: Any, path: str = "body") -> CreateRequest:
        data = jc.record(value, path, ("revision",), ("accept_blanks", "confirm_duplicate"))
        return cls(
            revision=jc.count(data["revision"], f"{path}.revision", MAX_REVISION),
            accept_blanks=jc.flag(data.get("accept_blanks", False), f"{path}.accept_blanks"),
            confirm_duplicate=jc.flag(
                data.get("confirm_duplicate", False), f"{path}.confirm_duplicate"
            ),
        )


@dataclass(frozen=True)
class CreatedObject:
    """One PwnDoc object this import created. ``audit_id`` is set for images and findings."""

    kind: str
    id: str
    at: str
    audit_id: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "at": self.at, "audit_id": self.audit_id}

    @classmethod
    def from_json(cls, value: Any, path: str = "object") -> CreatedObject:
        data = jc.record(value, path, ("kind", "id", "at", "audit_id"))
        kind = jc.choice(data["kind"], f"{path}.kind", OBJECT_KINDS)
        audit_id = jc.text(data["audit_id"], f"{path}.audit_id", 24, pattern=OBJECT_ID)
        if (kind in ("image", "finding")) != bool(audit_id):
            raise jc.fail(f"{path}.audit_id")
        return cls(
            kind=kind,
            id=jc.required(data["id"], f"{path}.id", 24, OBJECT_ID),
            at=jc.required(data["at"], f"{path}.at", 40, TIMESTAMP),
            audit_id=audit_id,
        )


@dataclass(frozen=True)
class Journal:
    """The undo journal: only ids this import itself created, appended as each is created."""

    import_id: str
    state: str
    updated: str
    objects: tuple[CreatedObject, ...] = ()
    version: int = 1

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "import_id": self.import_id,
            "state": self.state,
            "updated": self.updated,
            "objects": [o.to_json() for o in self.objects],
        }

    @classmethod
    def from_json(cls, value: Any, path: str = "journal") -> Journal:
        data = jc.record(value, path, ("version", "import_id", "state", "updated", "objects"))
        if data["version"] != 1:
            raise jc.fail(f"{path}.version")
        objects = jc.items(
            data["objects"], f"{path}.objects", MAX_JOURNAL_OBJECTS, CreatedObject.from_json
        )
        jc.unique(((o.kind, o.id) for o in objects), f"{path}.objects")  # type: ignore[misc]
        return cls(
            import_id=jc.required(data["import_id"], f"{path}.import_id", 32, IMPORT_ID),
            state=jc.choice(data["state"], f"{path}.state", JOURNAL_STATES),
            updated=jc.required(data["updated"], f"{path}.updated", 40, TIMESTAMP),
            objects=objects,
        )


@dataclass(frozen=True)
class ImportResult:
    """What a successful create produced (ids only, no names)."""

    audit_id: str
    company_id: str
    company_created: bool = False
    client_id: str = ""
    client_created: bool = False
    finding_ids: tuple[str, ...] = ()
    images_uploaded: int = 0  # new image rows journaled for undo
    images_reused: int = 0  # PwnDoc returned an existing row (same bytes); never deleted
    images_skipped: int = 0  # excluded, unusable or detached

    def to_json(self) -> dict[str, Any]:
        out = {name: getattr(self, name) for name in self.__dataclass_fields__}
        out["finding_ids"] = list(self.finding_ids)
        return out

    @classmethod
    def from_json(cls, value: Any, path: str = "result") -> ImportResult:
        data = jc.record(value, path, cls.__dataclass_fields__)
        oid = lambda name: jc.text(data[name], f"{path}.{name}", 24, pattern=OBJECT_ID)  # noqa: E731
        return cls(
            audit_id=jc.required(data["audit_id"], f"{path}.audit_id", 24, OBJECT_ID),
            company_id=jc.required(data["company_id"], f"{path}.company_id", 24, OBJECT_ID),
            company_created=jc.flag(data["company_created"], f"{path}.company_created"),
            client_id=oid("client_id"),
            client_created=jc.flag(data["client_created"], f"{path}.client_created"),
            finding_ids=jc.items(
                data["finding_ids"],
                f"{path}.finding_ids",
                MAX_FINDINGS,
                lambda v, p: jc.required(v, p, 24, OBJECT_ID),
            ),  # fmt: skip
            images_uploaded=jc.count(
                data["images_uploaded"], f"{path}.images_uploaded", MAX_IMAGES
            ),
            images_reused=jc.count(data["images_reused"], f"{path}.images_reused", MAX_IMAGES),
            images_skipped=jc.count(data["images_skipped"], f"{path}.images_skipped", MAX_IMAGES),
        )


@dataclass(frozen=True)
class UndoItem:
    kind: str
    id: str
    outcome: str  # "deleted", "gone" (already absent: 404), "kept" (403 image), "failed"

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "outcome": self.outcome}

    @classmethod
    def from_json(cls, value: Any, path: str = "item") -> UndoItem:
        data = jc.record(value, path, ("kind", "id", "outcome"))
        return cls(
            kind=jc.choice(data["kind"], f"{path}.kind", OBJECT_KINDS),
            id=jc.required(data["id"], f"{path}.id", 24, OBJECT_ID),
            outcome=jc.choice(data["outcome"], f"{path}.outcome", UNDO_OUTCOMES),
        )


@dataclass(frozen=True)
class UndoResult:
    items: tuple[UndoItem, ...] = ()

    def count(self, outcome: str, kind: str | None = None) -> int:
        return sum(1 for i in self.items if i.outcome == outcome and kind in (None, i.kind))

    def to_json(self) -> dict[str, Any]:
        return {"items": [i.to_json() for i in self.items]}

    @classmethod
    def from_json(cls, value: Any, path: str = "undo") -> UndoResult:
        data = jc.record(value, path, ("items",))
        return cls(
            jc.items(data["items"], f"{path}.items", MAX_JOURNAL_OBJECTS, UndoItem.from_json)
        )


@dataclass(frozen=True)
class ImportStatus:
    """Body of ``GET .../imports/{id}`` (and of every 202 answer). Persisted as status.json
    before work starts, so a restart can report ``interrupted``."""

    id: str
    file_id: str
    file_name: str
    state: str
    phase: str = ""
    total: int = 0
    done: int = 0
    current: str = ""  # fixed step label, e.g. "Uploading screenshots"
    error: str = ""  # fixed sentence from errors.py, or ""
    started: str = ""
    ended: str = ""
    revision: int = 0
    has_preview: bool = False
    result: ImportResult | None = None
    undo: UndoResult | None = None

    def to_json(self) -> dict[str, Any]:
        out = {name: getattr(self, name) for name in self.__dataclass_fields__}
        out["result"] = None if self.result is None else self.result.to_json()
        out["undo"] = None if self.undo is None else self.undo.to_json()
        return out

    @classmethod
    def from_json(cls, value: Any, path: str = "status") -> ImportStatus:
        data = jc.record(value, path, cls.__dataclass_fields__)
        stamp = lambda name: jc.text(data[name], f"{path}.{name}", 40, pattern=TIMESTAMP)  # noqa: E731
        return cls(
            id=jc.required(data["id"], f"{path}.id", 32, IMPORT_ID),
            file_id=jc.required(data["file_id"], f"{path}.file_id", 32, IMPORT_ID),
            file_name=jc.text(data["file_name"], f"{path}.file_name", MAX_FILE_NAME_CHARS),
            state=jc.choice(data["state"], f"{path}.state", IMPORT_STATES),
            phase=jc.choice(data["phase"], f"{path}.phase", PHASES),
            total=jc.count(data["total"], f"{path}.total"),
            done=jc.count(data["done"], f"{path}.done"),
            current=jc.text(data["current"], f"{path}.current", MAX_CURRENT_CHARS),
            error=jc.text(data["error"], f"{path}.error", MAX_ERROR_CHARS),
            started=stamp("started"),
            ended=stamp("ended"),
            revision=jc.count(data["revision"], f"{path}.revision", MAX_REVISION),
            has_preview=jc.flag(data["has_preview"], f"{path}.has_preview"),
            result=jc.optional(data["result"], ImportResult.from_json, f"{path}.result"),
            undo=jc.optional(data["undo"], UndoResult.from_json, f"{path}.undo"),
        )


@dataclass(frozen=True)
class Choice:
    value: str
    label: str

    def to_json(self) -> dict[str, Any]:
        return {"value": self.value, "label": self.label}

    @classmethod
    def from_json(cls, value: Any, path: str = "choice") -> Choice:
        data = jc.record(value, path, ("value", "label"))
        return cls(
            value=jc.text(data["value"], f"{path}.value", MAX_NAME_CHARS),
            label=jc.text(data["label"], f"{path}.label", MAX_NAME_CHARS),
        )


@dataclass(frozen=True)
class AuditTypeOption:
    name: str
    locales: tuple[str, ...] = ()  # locales the audit type has a template for

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "locales": list(self.locales)}

    @classmethod
    def from_json(cls, value: Any, path: str = "audit_type") -> AuditTypeOption:
        data = jc.record(value, path, ("name", "locales"))
        return cls(
            name=jc.text(data["name"], f"{path}.name", MAX_NAME_CHARS),
            locales=jc.items(
                data["locales"],
                f"{path}.locales",
                50,
                lambda v, p: jc.text(v, p, 32, pattern=LOCALE),
            ),
        )


@dataclass(frozen=True)
class PwnDocOptions:
    """Reference data read from PwnDoc with the user's cookie, offered as preview dropdowns."""

    languages: tuple[Choice, ...] = ()  # value = locale
    audit_types: tuple[AuditTypeOption, ...] = ()
    templates: tuple[Choice, ...] = ()  # value = template id

    def to_json(self) -> dict[str, Any]:
        return {
            "languages": [c.to_json() for c in self.languages],
            "audit_types": [a.to_json() for a in self.audit_types],
            "templates": [c.to_json() for c in self.templates],
        }

    @classmethod
    def from_json(cls, value: Any, path: str = "options") -> PwnDocOptions:
        data = jc.record(value, path, ("languages", "audit_types", "templates"))
        templates = jc.items(data["templates"], f"{path}.templates", MAX_OPTIONS, Choice.from_json)
        if any(not OBJECT_ID.fullmatch(t.value) for t in templates):
            raise jc.fail(f"{path}.templates")
        return cls(
            languages=jc.items(
                data["languages"], f"{path}.languages", MAX_OPTIONS, Choice.from_json
            ),
            audit_types=jc.items(
                data["audit_types"], f"{path}.audit_types", MAX_OPTIONS, AuditTypeOption.from_json
            ),
            templates=templates,
        )


@dataclass(frozen=True)
class PlanDefaults:
    """Preview defaults suggested from the user's PwnDoc data (``lookups.load_options``): the
    language, audit type and template of the user's most recent audit, else the first option."""

    language: str = ""
    audit_type: str = ""
    template_id: str = ""


@dataclass(frozen=True)
class NameCheck:
    """Result of ``lookups.check_names``: the existing company with that name (case-insensitive,
    ``""`` when it will be created) and whether a visible audit of that company has that name."""

    company_id: str = ""
    duplicate_audit: bool = False
