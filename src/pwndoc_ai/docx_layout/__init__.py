"""Layout validator and tidy for Word reports (blank pages, empty sections, orphans, fonts).

``inspect_layout`` lists the issues of a DOCX; ``tidy_docx`` fixes what is safely fixable and
returns the new bytes (the same bytes when nothing had to change). Both treat the file as
untrusted: hostile or unreadable packages raise ``DocxLayoutError`` with a user-safe message.
"""

from __future__ import annotations

from .errors import DocxLayoutError
from .fonts import fc_match
from .model import FontIssue, LayoutIssue, LayoutReport, TidyOptions, TidyResult
from .tidy import tidy_docx
from .validate import inspect_layout

__all__ = [
    "DocxLayoutError",
    "FontIssue",
    "LayoutIssue",
    "LayoutReport",
    "TidyOptions",
    "TidyResult",
    "fc_match",
    "inspect_layout",
    "tidy_docx",
]
