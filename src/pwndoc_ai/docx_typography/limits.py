"""Caps of the typography engine (docs/EXPORT_DESIGN.md (i) "Bounds")."""

from __future__ import annotations

from ..docx_engine import limits as engine_limits

MAX_PACKAGE_BYTES = 64 * 1024 * 1024  # larger packages are refused as ``too_large``
MAX_SECONDS = 20.0  # wall-clock budget of one apply_plan / fonts_used call
MAX_ELEMENTS = engine_limits.MAX_ELEMENTS
MAX_PARTS_PER_KIND = 20  # header, footer, footnotes and endnotes parts edited at most
MAX_FONT_USES = 20  # entries returned by fonts_used / sizes_used
MIN_HALF_POINTS = 12
MAX_HALF_POINTS = 144
WORD_DEFAULT_HALF_POINTS = 20  # w:sz absent everywhere: Word uses 10 pt
LINE_FIT = 1.2  # an exact line spacing below size x LINE_FIT would clip: switch to atLeast

# Fonts whose glyphs are pictograms: a run in one of these keeps its font (bullets, ticks...).
SYMBOL_FONTS = frozenset(
    {"symbol", "webdings", "marlett", "cambria math", "mt extra", "opensymbol"}
)
SYMBOL_PREFIXES = ("wingdings",)
MONO_HINTS = (
    "mono",
    "courier",
    "consolas",
    "menlo",
    "inconsolata",
    "source code",
    "fira code",
    "hack",
    "lucida console",
    "monaco",
)


def is_symbol_font(name: str | None) -> bool:
    if not name:
        return False
    key = name.strip().casefold()
    return key in SYMBOL_FONTS or key.startswith(SYMBOL_PREFIXES)


def is_mono_font(name: str | None) -> bool:
    if not name:
        return False
    key = name.strip().casefold()
    return any(hint in key for hint in MONO_HINTS)
