"""User-safe errors of the typography engine (fixed sentences, see ``export_errors``)."""

from __future__ import annotations

from ..export_errors import ExportInputError, ExportRefused, ExportUnavailable


class TypographyError(ExportRefused):
    """The document cannot be retypeset (HTTP 422). ``too_complex`` is raised by the budget;
    ``verify_failed`` when the output did not reopen or its text differs from the input."""

    default = "unreadable"
    MESSAGES = {
        "unreadable": "the document could not be read",
        "too_complex": "the document is too complex to change the fonts in time",
        "verify_failed": "the fonts could not be changed safely; the document was left as is",
        "too_large": "the document is too large to change the fonts",
    }


__all__ = ["ExportInputError", "ExportUnavailable", "TypographyError"]
