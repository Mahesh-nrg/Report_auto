"""Style lookup: the layout properties a paragraph inherits (keepNext, outline level, ...)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from lxml import etree

from .ooxml import MAX_OUTLINE, attr, ppr_props, w

MAX_CHAIN = 20
HEADING_NAME = re.compile(r"^heading ([1-9])$", re.IGNORECASE)
FONT_ATTRS = ("ascii", "hAnsi", "asciiTheme", "hAnsiTheme")


@dataclass(frozen=True)
class StyleInfo:
    style_id: str
    name: str
    kind: str
    based_on: str | None
    default: bool
    props: Mapping[str, object] = field(default_factory=dict)
    fonts: tuple[str, ...] = ()


def font_values(rpr: etree._Element | None) -> tuple[str, ...]:
    """The latin font names (and theme references) of an rPr."""
    fonts = rpr.find(w("rFonts")) if rpr is not None else None
    if fonts is None:
        return ()
    return tuple(v for name in FONT_ATTRS if (v := fonts.get(w(name))))


def _info(style: etree._Element) -> StyleInfo | None:
    style_id = attr(style, "styleId")
    if not style_id:
        return None
    name = style.find(w("name"))
    based = style.find(w("basedOn"))
    return StyleInfo(
        style_id=style_id,
        name=(attr(name, "val") or "") if name is not None else "",
        kind=attr(style, "type") or "paragraph",
        based_on=attr(based, "val") if based is not None else None,
        default=(attr(style, "default") or "0").lower() in ("1", "true", "on"),
        props=ppr_props(style.find(w("pPr"))),
        fonts=font_values(style.find(w("rPr"))),
    )


class StyleIndex:
    """Read-only view of styles.xml. A missing or empty part gives an index with no styles."""

    def __init__(self, root: etree._Element | None) -> None:
        self._styles: dict[str, StyleInfo] = {}
        self.default_fonts: tuple[str, ...] = ()
        if root is None:
            return
        for style in root.findall(w("style")):
            info = _info(style)
            if info is not None:
                self._styles.setdefault(info.style_id, info)
        defaults = root.find(f"{w('docDefaults')}/{w('rPrDefault')}/{w('rPr')}")
        self.default_fonts = font_values(defaults)

    def chain(self, style_id: str | None) -> list[StyleInfo]:
        """The style and its ancestors, nearest first (cycle and depth safe)."""
        found: list[StyleInfo] = []
        seen: set[str] = set()
        current = style_id
        while current and current not in seen and len(found) < MAX_CHAIN:
            seen.add(current)
            info = self._styles.get(current)
            if info is None:
                break
            found.append(info)
            current = info.based_on
        return found

    def default_style(self, kind: str) -> str | None:
        for info in self._styles.values():
            if info.default and info.kind == kind:
                return info.style_id
        return None

    def prop(self, style_id: str | None, key: str) -> object | None:
        for info in self.chain(style_id):
            if key in info.props:
                return info.props[key]
        return None

    def heading_name_level(self, style_id: str | None) -> int | None:
        for info in self.chain(style_id):
            match = HEADING_NAME.match(info.name)
            if match:
                return int(match.group(1))
        return None

    def is_caption(self, style_id: str | None) -> bool:
        return any(info.name.lower() == "caption" for info in self.chain(style_id))

    def used_fonts(self, used_ids: set[str]) -> tuple[str, ...]:
        """Fonts of the default styles, the used styles and their ancestors."""
        wanted = set(used_ids)
        for kind in ("paragraph", "character", "table"):
            default = self.default_style(kind)
            if default:
                wanted.add(default)
        fonts: list[str] = list(self.default_fonts)
        seen: set[str] = set()
        for style_id in sorted(wanted):
            for info in self.chain(style_id):
                if info.style_id not in seen:
                    seen.add(info.style_id)
                    fonts.extend(info.fonts)
        return tuple(fonts)


def outline_to_level(outline: object | None, name_level: int | None) -> int | None:
    """Heading level 1..9 from an outline level (0..8) or the style name; None for body text."""
    if isinstance(outline, int):
        return outline + 1 if 0 <= outline <= MAX_OUTLINE else None
    return name_level
