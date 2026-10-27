"""Read just enough of ``word/styles.xml`` to describe a block: name, outline level, list, size,
bold and font of a paragraph or table style (``basedOn`` chains resolved, depth capped)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from lxml import etree

from ..docx_engine.xmlutil import attr, w

MAX_CHAIN = 20
HEADING_NAME = re.compile(r"heading\s*([1-9])", re.IGNORECASE)
FALSE_VALUES = ("0", "false", "off")


@dataclass(frozen=True)
class StyleInfo:
    """Resolved properties of one style (``None``/``""`` = not set anywhere in the chain)."""

    name: str = ""
    level: int = 0
    numbered: bool = False
    size: float | None = None
    bold: bool | None = None
    font: str = ""


@dataclass(frozen=True)
class _Raw:
    name: str
    based_on: str
    level: int
    numbered: bool
    size: float | None
    bold: bool | None
    font: str


def on_off(element: etree._Element | None) -> bool | None:
    """A ``w:b``-like toggle: ``None`` when absent, else its value."""
    if element is None:
        return None
    return (attr(element, "val") or "true").lower() not in FALSE_VALUES


def half_points(rpr: etree._Element | None) -> float | None:
    node = rpr.find(w("sz")) if rpr is not None else None
    value = attr(node, "val") if node is not None else None
    if value is None or not value.isdigit() or not 2 <= int(value) <= 3276:
        return None
    return int(value) / 2


def ascii_font(rpr: etree._Element | None) -> str:
    node = rpr.find(w("rFonts")) if rpr is not None else None
    if node is None:
        return ""
    return (attr(node, "ascii") or attr(node, "hAnsi") or "").strip()


def outline_level(ppr: etree._Element | None) -> int:
    node = ppr.find(w("outlineLvl")) if ppr is not None else None
    value = attr(node, "val") if node is not None else None
    if value is None or not value.isdigit() or int(value) > 8:
        return 0
    return int(value) + 1


def _raw(style: etree._Element) -> _Raw:
    name_node = style.find(w("name"))
    based = style.find(w("basedOn"))
    ppr, rpr = style.find(w("pPr")), style.find(w("rPr"))
    return _Raw(
        name=(attr(name_node, "val") or "") if name_node is not None else "",
        based_on=(attr(based, "val") or "") if based is not None else "",
        level=outline_level(ppr),
        numbered=ppr is not None and ppr.find(w("numPr")) is not None,
        size=half_points(rpr),
        bold=on_off(rpr.find(w("b"))) if rpr is not None else None,
        font=ascii_font(rpr),
    )


def _resolve(style_id: str, raw: Mapping[str, _Raw], defaults: StyleInfo) -> StyleInfo:
    chain: list[_Raw] = []
    current = style_id
    while current in raw and len(chain) < MAX_CHAIN and raw[current] not in chain:
        chain.append(raw[current])
        current = raw[current].based_on
    first = chain[0] if chain else None
    level = next((item.level for item in chain if item.level), 0)
    match = HEADING_NAME.fullmatch(first.name.strip()) if first else None
    return StyleInfo(
        name=first.name if first else "",
        level=level or (int(match.group(1)) if match else 0),
        numbered=any(item.numbered for item in chain),
        size=next((item.size for item in chain if item.size), defaults.size),
        bold=next((item.bold for item in chain if item.bold is not None), None),
        font=next((item.font for item in chain if item.font), defaults.font),
    )


def _doc_defaults(root: etree._Element) -> StyleInfo:
    rpr = root.find(f"{w('docDefaults')}/{w('rPrDefault')}/{w('rPr')}")
    return StyleInfo(size=half_points(rpr), font=ascii_font(rpr))


@dataclass(frozen=True)
class StyleBook:
    """All resolved styles by id, plus the default paragraph style id."""

    styles: Mapping[str, StyleInfo]
    default_id: str = ""
    defaults: StyleInfo = StyleInfo()

    def get(self, style_id: str) -> StyleInfo:
        found = self.styles.get(style_id or self.default_id)
        return found if found is not None else self.defaults


def read_styles(root: etree._Element | None) -> StyleBook:
    """Build the style book from a parsed styles part (``None``: an empty book)."""
    if root is None:
        return StyleBook({})
    raw: dict[str, _Raw] = {}
    default_id = ""
    for style in root.iter(w("style")):
        style_id = attr(style, "styleId") or ""
        if not style_id or style_id in raw:
            continue
        raw[style_id] = _raw(style)
        is_default = (attr(style, "default") or "") in ("1", "true")
        if is_default and attr(style, "type") == "paragraph" and not default_id:
            default_id = style_id
    defaults = _doc_defaults(root)
    resolved = {key: _resolve(key, raw, defaults) for key in raw}
    return StyleBook(resolved, default_id, defaults)
