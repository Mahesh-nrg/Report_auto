"""Low-level rPr / pPr helpers: read and set fonts and sizes in schema order.

Only ``w:rFonts`` (``ascii``, ``hAnsi``, ``cs``; the ``*Theme`` latin and cs references are removed
on an edited element; ``eastAsia``, ``eastAsiaTheme`` and ``hint`` are kept), ``w:sz``/``w:szCs``
and ``w:spacing/@w:lineRule`` are ever written.
"""

from __future__ import annotations

from collections.abc import Mapping

from lxml import etree

from ..docx_engine.xmlutil import local, w
from .limits import LINE_FIT

FAMILY_ATTRS = ("ascii", "hAnsi", "cs")
THEME_ATTRS = ("asciiTheme", "hAnsiTheme", "cstheme")
RPR_ORDER = (
    "ins", "del", "moveFrom", "moveTo", "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps",
    "smallCaps", "strike", "dstrike", "outline", "shadow", "emboss", "imprint", "noProof",
    "snapToGrid", "vanish", "webHidden", "color", "spacing", "w", "kern", "position", "sz",
    "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign", "rtl", "cs", "em",
    "lang", "eastAsianLayout", "specVanish", "oMath",
)  # fmt: skip
LAST = len(RPR_ORDER) + 1  # rPrChange and foreign elements stay last
PPR_AFTER_RPR = ("sectPr", "pPrChange")
STYLE_AFTER_RPR = ("tblPr", "trPr", "tcPr", "tblStylePr")
ThemeFonts = Mapping[str, str]


def _rank(element: etree._Element) -> int:
    name = local(element)
    return RPR_ORDER.index(name) if name in RPR_ORDER else LAST


def child(parent: etree._Element, name: str) -> etree._Element:
    """The ``w:name`` child of an rPr, created at its schema position when absent."""
    found = parent.find(w(name))
    if found is not None:
        return found
    created = etree.Element(w(name))
    rank = RPR_ORDER.index(name)
    for sibling in parent:
        if isinstance(sibling.tag, str) and _rank(sibling) > rank:
            sibling.addprevious(created)
            return created
    parent.append(created)
    return created


def _insert_before(parent: etree._Element, created: etree._Element, names: tuple) -> None:
    for sibling in parent:
        if local(sibling) in names:
            sibling.addprevious(created)
            return
    parent.append(created)


def run_rpr(run: etree._Element) -> etree._Element:
    rpr = run.find(w("rPr"))
    if rpr is None:
        rpr = etree.Element(w("rPr"))
        run.insert(0, rpr)
    return rpr


def mark_rpr(paragraph: etree._Element) -> etree._Element:
    """The paragraph-mark rPr (``w:pPr/w:rPr``), created when absent."""
    ppr = paragraph.find(w("pPr"))
    if ppr is None:
        ppr = etree.Element(w("pPr"))
        paragraph.insert(0, ppr)
    rpr = ppr.find(w("rPr"))
    if rpr is None:
        rpr = etree.Element(w("rPr"))
        _insert_before(ppr, rpr, PPR_AFTER_RPR)
    return rpr


def style_rpr(style: etree._Element) -> etree._Element:
    rpr = style.find(w("rPr"))
    if rpr is None:
        rpr = etree.Element(w("rPr"))
        _insert_before(style, rpr, STYLE_AFTER_RPR)
    return rpr


def set_family(rpr: etree._Element, family: str) -> bool:
    """Write ``family`` as the latin and complex-script font. True when anything changed."""
    fonts = rpr.find(w("rFonts"))
    if fonts is not None and _has_family(fonts, family):
        return False
    fonts = child(rpr, "rFonts")
    for name in THEME_ATTRS:
        fonts.attrib.pop(w(name), None)
    for name in FAMILY_ATTRS:
        fonts.set(w(name), family)
    return True


def _has_family(fonts: etree._Element, family: str) -> bool:
    themed = any(fonts.get(w(name)) is not None for name in THEME_ATTRS)
    return not themed and all(fonts.get(w(name)) == family for name in FAMILY_ATTRS)


def set_size(rpr: etree._Element, half_points: int) -> bool:
    value = str(half_points)
    changed = False
    for name in ("sz", "szCs"):
        node = rpr.find(w(name))
        if node is None or node.get(w("val")) != value:
            child(rpr, name).set(w("val"), value)
            changed = True
    return changed


def explicit_family(rpr: etree._Element | None) -> bool:
    fonts = rpr.find(w("rFonts")) if rpr is not None else None
    if fonts is None:
        return False
    return any(fonts.get(w(name)) is not None for name in FAMILY_ATTRS + THEME_ATTRS)


def explicit_size(rpr: etree._Element | None) -> bool:
    return rpr is not None and (rpr.find(w("sz")) is not None or rpr.find(w("szCs")) is not None)


def latin(rpr: etree._Element | None, theme: ThemeFonts) -> str | None:
    """The latin font an rPr asks for (theme references resolved), None when it sets none."""
    fonts = rpr.find(w("rFonts")) if rpr is not None else None
    if fonts is None:
        return None
    for name in ("ascii", "hAnsi"):
        if fonts.get(w(name)):
            return fonts.get(w(name))
    for name in ("asciiTheme", "hAnsiTheme"):
        ref = fonts.get(w(name))
        if ref:
            return theme.get(ref) or None
    return None


def size(rpr: etree._Element | None) -> int | None:
    node = rpr.find(w("sz")) if rpr is not None else None
    try:
        return int(node.get(w("val"))) if node is not None else None
    except (TypeError, ValueError):
        return None


def fit_line(ppr: etree._Element | None, half_points: int) -> bool:
    """An exact line spacing too small for ``half_points`` becomes ``atLeast`` (never clips)."""
    spacing = ppr.find(w("spacing")) if ppr is not None else None
    if spacing is None or spacing.get(w("lineRule")) != "exact":
        return False
    try:
        line = int(spacing.get(w("line")) or "0")
    except ValueError:
        line = 0
    if line >= half_points * 10 * LINE_FIT:  # twips = pt * 20 = half-points * 10
        return False
    spacing.set(w("lineRule"), "atLeast")
    return True
