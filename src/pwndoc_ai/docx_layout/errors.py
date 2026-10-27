"""Errors raised by the layout engine. Messages are short and safe to show to a user."""

from __future__ import annotations


class DocxLayoutError(ValueError):
    """The document cannot be read or is hostile. The message is user-safe: it never contains a
    file name, a path, a URL or text taken from the document."""
