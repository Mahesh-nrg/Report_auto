"""WordprocessingML helpers shared by the layout modules."""

from __future__ import annotations

from lxml import etree

from pwndoc_ai.docx_engine.xmlutil import W_NS, XML_NS, local, parse_xml, w

__all__ = ["W_NS", "XML_NS", "attr", "is_on", "local", "parse_xml", "ppr_props", "w"]

OFF_VALUES = frozenset({"0", "false", "off", "none"})
NO_BORDER = frozenset({"nil", "none"})
MAX_OUTLINE = 8  # outline levels 0..8 are headings, 9 is body text


def attr(element: etree._Element, name: str) -> str | None:
    return element.get(w(name))


def is_on(element: etree._Element | None) -> bool | None:
    """An on/off property: None when the element is absent, else its effective value."""
    if element is None:
        return None
    value = element.get(w("val"))
    return value is None or value.lower() not in OFF_VALUES


def _numbered(ppr: etree._Element) -> bool | None:
    num_pr = ppr.find(w("numPr"))
    num_id = num_pr.find(w("numId")) if num_pr is not None else None
    if num_id is None:
        return None
    return (num_id.get(w("val")) or "0") != "0"


def _num_ref(ppr: etree._Element) -> tuple[str | None, int | None] | None:
    """(numId, ilvl) of a numPr, each None when it is not given there."""
    num_pr = ppr.find(w("numPr"))
    if num_pr is None:
        return None
    ident, level = num_pr.find(w("numId")), num_pr.find(w("ilvl"))
    try:
        ilvl = int(level.get(w("val"))) if level is not None else None
    except (TypeError, ValueError):
        ilvl = None
    return (ident.get(w("val")) if ident is not None else None), ilvl


def _decorated(ppr: etree._Element) -> bool | None:
    borders = ppr.find(w("pBdr"))
    shading = ppr.find(w("shd"))
    framed = ppr.find(w("framePr")) is not None
    boxed = borders is not None and any(
        (child.get(w("val")) or "single") not in NO_BORDER for child in borders
    )
    filled = shading is not None and (shading.get(w("fill")) or "auto") != "auto"
    if framed or boxed or filled:
        return True
    return None


def _outline(ppr: etree._Element) -> int | None:
    node = ppr.find(w("outlineLvl"))
    try:
        return int(node.get(w("val"))) if node is not None else None
    except (TypeError, ValueError):
        return None


def ppr_props(ppr: etree._Element | None) -> dict[str, object]:
    """The explicitly set layout properties of a pPr: keepNext, pageBreakBefore (bool),
    outlineLvl (int), numbered, decor (bool), numRef ((numId, ilvl)). Absent ones are not there."""
    if ppr is None:
        return {}
    found = {
        "keepNext": is_on(ppr.find(w("keepNext"))),
        "pageBreakBefore": is_on(ppr.find(w("pageBreakBefore"))),
        "outlineLvl": _outline(ppr),
        "numbered": _numbered(ppr),
        "numRef": _num_ref(ppr),
        "decor": _decorated(ppr),
    }
    return {key: value for key, value in found.items() if value is not None}
