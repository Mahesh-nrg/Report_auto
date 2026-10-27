"""Paragraph style lookup (names, outline levels, fonts) from word/styles.xml."""

from __future__ import annotations

from dataclasses import dataclass

from lxml import etree

from .xmlutil import attr, w

MAX_BASED_ON_DEPTH = 10


@dataclass(frozen=True)
class StyleInfo:
    style_id: str
    name: str
    based_on: str | None
    outline_level: int | None  # 0-8 as stored, None when unset
    font: str | None  # ascii/hAnsi font of the style itself


class StyleMap:
    """Read-only view of the styles part. An empty map is valid (documents without styles)."""

    def __init__(self, styles: dict[str, StyleInfo], default_paragraph: str | None) -> None:
        self._styles = styles
        self.default_paragraph = default_paragraph

    @classmethod
    def empty(cls) -> StyleMap:
        return cls({}, None)

    @classmethod
    def from_root(cls, root: etree._Element) -> StyleMap:
        styles: dict[str, StyleInfo] = {}
        default = None
        for element in root.iter(w("style")):
            if attr(element, "type") not in (None, "paragraph"):
                continue
            info = _style_info(element)
            if info is None:
                continue
            styles[info.style_id] = info
            if attr(element, "default") in ("1", "true"):
                default = info.style_id
        return cls(styles, default)

    def name(self, style_id: str | None) -> str:
        style_id = style_id or self.default_paragraph
        if style_id is None:
            return "Normal"
        info = self._styles.get(style_id)
        return info.name if info else style_id

    def chain(self, style_id: str | None) -> list[StyleInfo]:
        """The style and its ancestors (bounded, cycle-safe)."""
        out: list[StyleInfo] = []
        current = style_id or self.default_paragraph
        while current and len(out) < MAX_BASED_ON_DEPTH:
            info = self._styles.get(current)
            if info is None or info in out:
                break
            out.append(info)
            current = info.based_on
        return out

    def outline_level(self, style_id: str | None) -> int | None:
        for info in self.chain(style_id):
            if info.outline_level is not None:
                return info.outline_level
        return None

    def font(self, style_id: str | None) -> str | None:
        for info in self.chain(style_id):
            if info.font:
                return info.font
        return None


def _style_info(element: etree._Element) -> StyleInfo | None:
    style_id = attr(element, "styleId")
    if not style_id:
        return None
    name = element.find(w("name"))
    based = element.find(w("basedOn"))
    outline = element.find(f"{w('pPr')}/{w('outlineLvl')}")
    fonts = element.find(f"{w('rPr')}/{w('rFonts')}")
    return StyleInfo(
        style_id=style_id,
        name=(attr(name, "val") if name is not None else None) or style_id,
        based_on=attr(based, "val") if based is not None else None,
        outline_level=_level(outline),
        font=(attr(fonts, "ascii") or attr(fonts, "hAnsi")) if fonts is not None else None,
    )


def _level(element: etree._Element | None) -> int | None:
    if element is None:
        return None
    try:
        value = int(attr(element, "val") or "")
    except ValueError:
        return None
    return value if 0 <= value <= 9 else None
