"""Scanned-page detection from the spans and image boxes mutool reports.

A page is ``text`` with at least ``MIN_TEXT_CHARS`` alphanumeric characters, ``image_only``
when images cover at least ``IMAGE_COVER`` of it, else ``blank``. The share of image-only pages
among the first ``TEXT_LAYER_PAGES`` gives the verdict (``model.verdict_for``).
"""

from __future__ import annotations

from collections.abc import Sequence

from . import limits
from .extract import Page

PAGE_KINDS = ("text", "image_only", "blank")


def page_kind(page: Page) -> str:
    chars = sum(sum(1 for ch in span.text if ch.isalnum()) for span in page.spans)
    if chars >= limits.MIN_TEXT_CHARS:
        return "text"
    area = sum(max(0.0, (b[2] - b[0])) * max(0.0, (b[3] - b[1])) for b in page.images)
    if area >= limits.IMAGE_COVER * page.width * page.height:
        return "image_only"
    return "blank"


def scanned_share(pages: Sequence[Page]) -> float:
    """Share of image-only pages among the first ``TEXT_LAYER_PAGES`` (0.0 for no pages)."""
    sample = list(pages[: limits.TEXT_LAYER_PAGES])
    if not sample:
        return 0.0
    image_only = sum(1 for page in sample if page_kind(page) == "image_only")
    return round(image_only / len(sample), 4)


def has_text(pages: Sequence[Page]) -> bool:
    return any(page_kind(page) == "text" for page in pages)


__all__ = ["PAGE_KINDS", "has_text", "page_kind", "scanned_share"]
