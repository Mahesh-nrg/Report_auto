"""Errors of the Export tab (fonts, typography, section detection, PDF import).

Every message is a fixed sentence that is safe to show a user. A message never contains a file
name, a path, a URL, a client name, a font name typed by a user or any text taken from a
document: the sentence is chosen from the ``MESSAGES`` table of the class by a short ``code``.
The HTTP layer answers ``{"detail": str(exc)}`` with ``exc.status``. ``ExportInputError`` may
name the field that failed validation, but only as a schema path built from the contract's own
key names (``plan.by_section.s12.all.size``), never from user text.

Contract: docs/EXPORT_DESIGN.md. The package errors (``docx_typography.errors``,
``docx_sections.errors``, ``pdf_import.errors``) subclass these classes.
"""

from __future__ import annotations

import re
from typing import ClassVar

FIELD_PATH = re.compile(
    r"[a-z][a-z0-9_]{0,40}(?:\[\d{1,5}\])?(?:\.[a-z][a-z0-9_]{0,40}(?:\[\d{1,5}\])?){0,7}"
)


class ExportFailure(Exception):
    """Base class. ``code`` picks one fixed sentence from ``MESSAGES``; ``status`` is the HTTP
    status the routes answer with. An unknown code falls back to ``default``."""

    status: ClassVar[int] = 500
    default: ClassVar[str] = "failed"
    MESSAGES: ClassVar[dict[str, str]] = {"failed": "the export step failed; see the gateway log"}

    def __init__(self, code: str | None = None) -> None:
        chosen = code if code is not None else self.default
        if chosen not in self.MESSAGES:
            chosen = self.default
        self.code = chosen
        super().__init__(self.MESSAGES[chosen])

    def __str__(self) -> str:
        return str(self.args[0])


class ExportInputError(ExportFailure, ValueError):
    """A request body or a stored record failed strict validation (HTTP 400)."""

    status = 400
    default = "invalid"
    MESSAGES = {
        "invalid": "the request is not valid",
        "font_name": "the font name is not valid",
        "font_size": "the font size must be between 6 and 72 points, in steps of 0.5",
        "unknown_section": "the plan names a section that is not in this document",
        "engine": "unknown PDF conversion engine",
    }

    def __init__(self, code: str | None = None, *, field: str = "") -> None:
        super().__init__(code)
        self.field = field if FIELD_PATH.fullmatch(field or "") else ""
        if self.field:
            self.args = (f"{self.MESSAGES[self.code]}: {self.field}",)


class ExportNotFound(ExportFailure, LookupError):
    """Unknown file, or one that belongs to another user (HTTP 404)."""

    status = 404
    default = "file"
    MESSAGES = {"file": "file not found", "sections": "this file has not been analysed yet"}


class ExportConflict(ExportFailure):
    """Wrong state, stale revision or a busy server (HTTP 409)."""

    status = 409
    default = "running"
    MESSAGES = {
        "running": "this file is still being analysed; wait for it or cancel it",
        "not_ready": "analyse this file before choosing fonts or downloading",
        "stale": "the font plan changed in another tab; reload it and try again",
        "busy_user": "you already have an analysis running; wait for it or cancel it",
        "busy_server": "the server is busy with other analyses; try again shortly",
        "too_many": "you have too many export files; delete some first",
    }


class ExportCancelled(ExportFailure):
    """The user cancelled the analysis (HTTP 409 when surfaced)."""

    status = 409
    default = "cancelled"
    MESSAGES = {"cancelled": "the analysis was cancelled"}


class ExportLimitError(ExportFailure):
    """The document is over a size cap (HTTP 413)."""

    status = 413
    default = "too_large"
    MESSAGES = {
        "too_large": "the file is too large",
        "quota": "your export storage is full; delete some files first",
        "too_many_pages": "the PDF has too many pages",
        "too_many_blocks": "the document has too many paragraphs to analyse",
    }


class ExportUnsupported(ExportFailure):
    """The upload is neither a DOCX nor a PDF (HTTP 415)."""

    status = 415
    default = "type"
    MESSAGES = {"type": "upload a .docx or a .pdf file"}


class ExportRefused(ExportFailure, ValueError):
    """The document is readable but refused by policy or is unusable (HTTP 422)."""

    status = 422
    default = "unreadable"
    MESSAGES = {"unreadable": "the document could not be read"}


class ExportUnavailable(ExportFailure, RuntimeError):
    """A needed package or tool is not installed (HTTP 501)."""

    status = 501
    default = "docx"
    MESSAGES = {
        "docx": "the Export tab is not installed: pip install 'pwndoc-ai[docx]'",
        "pdf_tools": "PDF import needs mutool and pdftotext (packages mupdf-tools, poppler-utils)",
        "pdf2docx": (
            "the pdf2docx engine is not installed: pip install 'pwndoc-ai[pdfimport]'"
            " (PyMuPDF is AGPL-3.0 licensed)"
        ),
        "renderer": "PDF download is not available: LibreOffice is not installed",
    }


class ExportUpstreamError(ExportFailure):
    """The LLM or a converter failed (HTTP 502). Details go to the log by exception type only."""

    status = 502
    default = "failed"
    MESSAGES = {
        "failed": "the conversion failed; see the gateway log",
        "llm": "the AI model failed; the sections were detected without it",
        "llm_key": "the LLM rejected the API key; an administrator must fix it in AI Settings",
        "rejected": "the AI answer was rejected and not used",
    }


__all__ = [
    "FIELD_PATH",
    "ExportCancelled",
    "ExportConflict",
    "ExportFailure",
    "ExportInputError",
    "ExportLimitError",
    "ExportNotFound",
    "ExportRefused",
    "ExportUnavailable",
    "ExportUnsupported",
    "ExportUpstreamError",
]
