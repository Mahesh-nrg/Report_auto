"""Errors raised by the DOCX engine. Messages are short and safe to show to a user."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import RefineResult


class DocxError(ValueError):
    """The document (or the request) cannot be processed. The message is user-safe: it never
    contains a file name, a path, a URL or any text taken from the document."""


class RefineCancelled(Exception):
    """The run was cancelled between chunks.

    ``result`` holds everything finished before the cancel (a complete, valid pair of documents
    with the changes made so far), so the caller can keep it or throw it away.
    """

    def __init__(self, result: RefineResult) -> None:
        super().__init__("refinement cancelled")
        self.result = result
