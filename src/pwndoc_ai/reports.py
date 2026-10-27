"""Helpers for the Word/PDF report download: naming, pre-flight check, response headers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

DOCX_MAGIC = b"PK"  # a .docx is a zip archive
PDF_MAGIC = b"%PDF"
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_TYPE = "application/pdf"
FORMATS = {"docx": ("docx", DOCX_TYPE), "pdf": ("pdf", PDF_TYPE)}
NO_TEMPLATE = (
    "This audit has no report template for its language, so no Word report can be built. In "
    "PwnDoc open Data > Audit Types, add a template for this audit's language, then try again."
)
UNSAFE_FILENAME = re.compile(r"[^\w.\- ]+")  # \w is unicode-aware: keeps non-Latin letters
NON_ASCII_NAME = re.compile(r"[^A-Za-z0-9._ -]+")
MAX_FILENAME = 80


def report_stem(audit_name: Any) -> str:
    """A download name stem that cannot carry path separators, quotes or header-breaking chars."""
    cleaned = UNSAFE_FILENAME.sub("_", str(audit_name or "")).strip(" ._")[:MAX_FILENAME]
    return cleaned or "report"


def export_blocker(audit: Mapping[str, Any]) -> str:
    """Why this audit cannot be exported right now ('' when it can)."""
    return "" if audit.get("template") else NO_TEMPLATE


def is_docx(data: object) -> bool:
    return isinstance(data, bytes) and data.startswith(DOCX_MAGIC)


def content_disposition(stem: str, extension: str) -> str:
    """Attachment header with an ASCII fallback plus the real name (RFC 5987)."""
    fallback = NON_ASCII_NAME.sub("_", stem).strip(" ._") or "report"
    return (
        f'attachment; filename="{fallback}.{extension}"; '
        f"filename*=UTF-8''{quote(f'{stem}.{extension}', safe='')}"
    )
