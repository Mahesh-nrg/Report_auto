"""Errors of the DOCX importer. Every message is a fixed sentence that is safe to show a user.

A message never contains a file name, a path, a URL, a client name or any text taken from the
document: the text is chosen from the ``MESSAGES`` table of the class by a short ``code``. The
HTTP layer answers ``{"detail": str(exc)}`` with ``exc.status``. ``ImportInputError`` may name the
field that failed validation, but only as a schema path built from the importer's own key names
(``findings[3].title``), never from user text.
"""

from __future__ import annotations

import re
from typing import ClassVar

FIELD_PATH = re.compile(
    r"[a-z][a-z0-9_]{0,40}(?:\[\d{1,5}\])?(?:\.[a-z][a-z0-9_]{0,40}(?:\[\d{1,5}\])?){0,6}"
)


class ImportFailure(Exception):
    """Base class. ``code`` picks one fixed sentence from ``MESSAGES``; ``status`` is the HTTP
    status the routes answer with."""

    status: ClassVar[int] = 500
    default: ClassVar[str] = "failed"
    MESSAGES: ClassVar[dict[str, str]] = {"failed": "the import failed; see the gateway log"}

    def __init__(self, code: str | None = None) -> None:
        chosen = code if code is not None else self.default
        if chosen not in self.MESSAGES:
            chosen = self.default
        self.code = chosen
        super().__init__(self.MESSAGES[chosen])


class ImportInputError(ImportFailure, ValueError):
    """A request body or a stored record failed strict validation (HTTP 400)."""

    status = 400
    default = "invalid"
    MESSAGES = {
        "invalid": "the request is not valid",
        "blanks": "some fields are blank: fill them in or accept the blanks before creating",
        "blockers": "fix the problems marked as required before creating",
        "unknown_key": "the request names a finding or screenshot that is not in this import",
        "image_not_usable": "this screenshot cannot be uploaded to PwnDoc",
    }

    def __init__(self, code: str | None = None, *, field: str = "") -> None:
        super().__init__(code)
        self.field = field if FIELD_PATH.fullmatch(field or "") else ""
        if self.field and self.code == "invalid":
            self.args = (f"invalid value: {self.field}",)

    def __str__(self) -> str:
        return str(self.args[0])


class DocumentUnreadable(ImportFailure, ValueError):
    """The file is not a report this importer can read (HTTP 400)."""

    status = 400
    default = "unreadable"
    MESSAGES = {
        "unreadable": "the document could not be read",
        "not_template": "this document does not look like a report from the active PwnDoc template",
        "no_findings": "no findings were found in this document",
    }


class ImportNotFound(ImportFailure, LookupError):
    """Unknown import, or one that belongs to another user (HTTP 404)."""

    status = 404
    default = "import"
    MESSAGES = {
        "import": "import not found",
        "file": "file not found",
        "image": "screenshot not found",
    }


class ImportConflict(ImportFailure):
    """The import is in the wrong state, or the server is busy (HTTP 409)."""

    status = 409
    default = "busy_user"
    MESSAGES = {
        "busy_user": "you already have an import step running; wait for it or cancel it",
        "busy_server": "the server is busy with other imports; try again shortly",
        "running": "this import is still running; cancel it first",
        "stale": "the preview changed in another tab; reload it and try again",
        "not_ready": "this import has no preview to change or create",
        "already_created": "this import was already created in PwnDoc",
        "duplicate_audit": "an audit with this name already exists for this company",
        "nothing_to_undo": "there is nothing this import created that can be undone",
        "changed_since_import": (
            "the audit has findings this import did not create, so nothing was deleted;"
            " remove them first or delete the audit in PwnDoc"
        ),
        "too_many": "you have too many imports; delete some first",
    }


class ImportLimitError(ImportFailure):
    """The document or the import is over a size cap (HTTP 413)."""

    status = 413
    default = "too_large"
    MESSAGES = {
        "too_large": "the document is too large to import",
        "too_many_findings": "the document has more findings than one import can create",
        "too_many_images": "the document has more screenshots than one import can upload",
        "quota": "your import storage is full; delete some imports first",
    }


class ImportSessionExpired(ImportFailure):
    """PwnDoc answered 401 during a step (HTTP 401)."""

    status = 401
    default = "expired"
    MESSAGES = {"expired": "your PwnDoc session expired; sign in again and retry"}


class ImportDenied(ImportFailure):
    """PwnDoc answered 403 (HTTP 403)."""

    status = 403
    default = "denied"
    MESSAGES = {"denied": "PwnDoc refused this action for your account"}


class ImportUpstreamError(ImportFailure):
    """PwnDoc or the LLM failed (HTTP 502). Details go to the log by exception type only."""

    status = 502
    default = "pwndoc"
    MESSAGES = {
        "pwndoc": "PwnDoc request failed; see the gateway log",
        "llm": "the AI model failed; the fields it should fill were left blank",
        "llm_key": "the LLM rejected the API key; an administrator must fix it in AI Settings",
    }


class FallbackRejected(ImportFailure):
    """An LLM fallback answer failed validation; the caller skips it and keeps the blank."""

    status = 502
    default = "rejected"
    MESSAGES = {"rejected": "the AI answer was rejected and not used"}


class ImportCancelled(ImportFailure):
    """The user cancelled a running step (HTTP 409 when surfaced)."""

    status = 409
    default = "cancelled"
    MESSAGES = {"cancelled": "the import step was cancelled"}


class ImportEngineMissing(ImportFailure, RuntimeError):
    """lxml or the DOCX engine is not installed (HTTP 501)."""

    status = 501
    default = "missing"
    MESSAGES = {"missing": "document import is not installed: pip install 'pwndoc-ai[docx]'"}


__all__ = [
    "DocumentUnreadable",
    "FallbackRejected",
    "ImportCancelled",
    "ImportConflict",
    "ImportDenied",
    "ImportEngineMissing",
    "ImportFailure",
    "ImportInputError",
    "ImportLimitError",
    "ImportNotFound",
    "ImportSessionExpired",
    "ImportUpstreamError",
]
