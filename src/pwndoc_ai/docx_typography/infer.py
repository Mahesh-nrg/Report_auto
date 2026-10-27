"""Block types without section detection (audit downloads): style name, outline level, fonts."""

from __future__ import annotations

from lxml import etree

from ..block_types import heading_type
from ..docx_engine.xmlutil import local, w
from . import rpr as rp
from .blocks import body_blocks
from .limits import is_mono_font
from .stylebook import StyleBook

MAX_OUTLINE = 8


def _outline(paragraph: etree._Element) -> int | None:
    node = paragraph.find(f"{w('pPr')}/{w('outlineLvl')}")
    try:
        level = int(node.get(w("val"))) if node is not None else None
    except (TypeError, ValueError):
        return None
    return level if level is not None and 0 <= level <= MAX_OUTLINE else None


def _all_mono(paragraph: etree._Element, book: StyleBook, theme: rp.ThemeFonts) -> bool:
    fonts = []
    for run in paragraph.iter(w("r")):
        if "".join(t.text or "" for t in run.iter(w("t"))).strip():
            fonts.append(rp.latin(run.find(w("rPr")), theme))
    if not fonts:
        return False
    fallback = book.effective(book.paragraph_style(paragraph), theme).family
    return all(is_mono_font(font or fallback) for font in fonts)


def infer_type(element: etree._Element, book: StyleBook, theme: rp.ThemeFonts) -> str:
    """The block type of one body block: table; the style's type; an outline level; code when
    every text run is in a monospace font; body."""
    if local(element) == "tbl":
        return "table"
    kind = book.style_type(book.paragraph_style(element))
    if kind != "body":
        return kind
    level = _outline(element)
    if level is not None:
        return heading_type(level + 1)
    return "code" if _all_mono(element, book, theme) else "body"


def infer_types(root: etree._Element, book: StyleBook, theme: rp.ThemeFonts) -> dict[str, str]:
    """``{block_id: block_type}`` for every body block of a ``w:document``."""
    return {ident: infer_type(item, book, theme) for ident, item in body_blocks(root)}
