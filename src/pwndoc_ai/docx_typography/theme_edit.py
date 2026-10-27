"""Theme fonts: read the major/minor latin typefaces and, for a document-wide default, set them."""

from __future__ import annotations

from lxml import etree

from .model import TypographyPlan

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
SCHEME = f"{{{A_NS}}}themeElements/{{{A_NS}}}fontScheme"
REFS = {
    "major": ("majorAscii", "majorHAnsi", "majorBidi"),
    "minor": ("minorAscii", "minorHAnsi", "minorBidi"),
}


def _latin(theme: etree._Element | None, kind: str) -> etree._Element | None:
    if theme is None:
        return None
    return theme.find(f"{SCHEME}/{{{A_NS}}}{kind}Font/{{{A_NS}}}latin")


def theme_fonts(theme: etree._Element | None) -> dict[str, str]:
    """``{"minorHAnsi": "Calibri", ...}`` for the rFonts ``*Theme`` attribute values."""
    found: dict[str, str] = {}
    for kind, refs in REFS.items():
        node = _latin(theme, kind)
        typeface = (node.get("typeface") or "") if node is not None else ""
        if typeface:
            found.update(dict.fromkeys(refs, typeface))
    return found


def edit_theme(theme: etree._Element | None, plan: TypographyPlan) -> bool:
    """Point the theme at the plan's families (only for a plan without sections and with a
    default family). True when the theme changed."""
    if theme is None or plan.by_section or not plan.default.family:
        return False
    wanted = {
        "minor": plan.resolve("body").family,
        "major": plan.resolve("heading_1").family or plan.default.family,
    }
    changed = False
    for kind, family in wanted.items():
        node = _latin(theme, kind)
        if node is not None and family and node.get("typeface") != family:
            node.set("typeface", family)
            changed = True
    return changed
