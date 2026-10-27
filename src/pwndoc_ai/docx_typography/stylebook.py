"""A live view of styles.xml: style chains, the block type a style stands for, effective fonts.

Values are read from the (possibly edited) elements on every call, so the same book answers
"before" and "after" questions. A document without a styles part gets an empty book.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lxml import etree

from ..block_types import heading_type
from ..docx_engine.xmlutil import w
from . import rpr as rp
from .limits import WORD_DEFAULT_HALF_POINTS

MAX_CHAIN = 20
HEADING_NAME = re.compile(r"heading\s*([1-9])")
TOC_NAME = re.compile(r"toc\s*([1-9]|heading)")
FIXED_NAMES = {
    "title": "title",
    "caption": "caption",
    "quote": "quote",
    "intense quote": "quote",
    "header": "header",
    "footer": "footer",
    "footnote text": "footnote",
    "endnote text": "footnote",
    "normal": "body",
    "plain text": "code",
    "html preformatted": "code",
}


@dataclass(frozen=True)
class Effective:
    """The family (theme resolved) and size in half-points a style ends up with."""

    family: str | None
    size: int | None


def name_type(name: str) -> str | None:
    """The block type a style name stands for, None for names that say nothing."""
    key = " ".join(name.casefold().split())
    if key in FIXED_NAMES:
        return FIXED_NAMES[key]
    heading = HEADING_NAME.fullmatch(key)
    if heading:
        return heading_type(int(heading.group(1)))
    if TOC_NAME.fullmatch(key):
        return "toc"
    if key.startswith("list"):
        return "list"
    if "code" in key or "preformatted" in key:
        return "code"
    return None


class StyleBook:
    def __init__(self, root: etree._Element | None) -> None:
        self.root = root
        self.styles: dict[str, etree._Element] = {}
        self._types: dict[str | None, str] = {}
        self.default_paragraph: str | None = None
        for style in root.findall(w("style")) if root is not None else ():
            ident = style.get(w("styleId"))
            if ident and ident not in self.styles:
                self.styles[ident] = style
                if style.get(w("type")) == "paragraph" and _on(style.get(w("default"))):
                    self.default_paragraph = ident

    def chain(self, ident: str | None) -> list[etree._Element]:
        found: list[etree._Element] = []
        seen: set[str] = set()
        while ident and ident not in seen and len(found) < MAX_CHAIN:
            seen.add(ident)
            style = self.styles.get(ident)
            if style is None:
                break
            found.append(style)
            based = style.find(w("basedOn"))
            ident = based.get(w("val")) if based is not None else None
        return found

    def own_type(self, ident: str) -> str | None:
        name = self.styles[ident].find(w("name"))
        return name_type(name.get(w("val")) or "") if name is not None else None

    def style_type(self, ident: str | None) -> str:
        """The block type of a paragraph style (its own name, then its ancestors); body."""
        key = ident or self.default_paragraph
        if key not in self._types:
            names = (self.own_type(s.get(w("styleId"))) for s in self.chain(key))
            self._types[key] = next((t for t in names if t), "body")
        return self._types[key]

    def defaults_rpr(self, create: bool = False) -> etree._Element | None:
        if self.root is None:
            return None
        path = f"{w('docDefaults')}/{w('rPrDefault')}/{w('rPr')}"
        found = self.root.find(path)
        if found is not None or not create:
            return found
        defaults = self.root.find(w("docDefaults"))
        if defaults is None:
            defaults = etree.Element(w("docDefaults"))
            self.root.insert(0, defaults)
        holder = defaults.find(w("rPrDefault"))
        if holder is None:
            holder = etree.Element(w("rPrDefault"))
            defaults.insert(0, holder)
        return rp.style_rpr(holder)

    def chain_values(self, ident: str | None, theme: rp.ThemeFonts) -> Effective:
        """Family and size set by the style chain itself (no docDefaults)."""
        family = size = None
        for style in self.chain(ident):
            props = style.find(w("rPr"))
            family = family or rp.latin(props, theme)
            size = size if size is not None else rp.size(props)
        return Effective(family, size)

    def effective(self, ident: str | None, theme: rp.ThemeFonts) -> Effective:
        own = self.chain_values(ident, theme)
        defaults = self.defaults_rpr()
        family = own.family or rp.latin(defaults, theme)
        size = own.size if own.size is not None else rp.size(defaults)
        return Effective(family, WORD_DEFAULT_HALF_POINTS if size is None else size)

    def paragraph_style(self, paragraph: etree._Element) -> str | None:
        node = paragraph.find(f"{w('pPr')}/{w('pStyle')}")
        ident = node.get(w("val")) if node is not None else None
        return ident if ident in self.styles else self.default_paragraph


def _on(value: str | None) -> bool:
    return (value or "0").lower() in ("1", "true", "on")
