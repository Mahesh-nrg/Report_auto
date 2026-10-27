"""User-safe errors of section detection (fixed sentences, see ``export_errors``)."""

from __future__ import annotations

from ..export_errors import ExportCancelled, ExportLimitError, ExportRefused, ExportUpstreamError


class SectionsError(ExportRefused):
    """The document has nothing to detect sections in, or cannot be read (HTTP 422)."""

    default = "unreadable"
    MESSAGES = {
        "unreadable": "the document could not be read",
        "no_text": "the document has no text, so no sections can be detected",
        "too_complex": "the document is too complex to analyse in time",
    }


class LabelAnswerRejected(ExportUpstreamError):
    """One LLM answer failed validation (unknown id, duplicate, bad type, leftover placeholder,
    too long). The detector repairs the batch with the heuristic; it is never shown as is."""

    default = "rejected"


__all__ = [
    "ExportCancelled",
    "ExportLimitError",
    "LabelAnswerRejected",
    "SectionsError",
]
