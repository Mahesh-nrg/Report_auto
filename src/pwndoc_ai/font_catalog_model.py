"""Data model of the font catalog: font names, font sizes and the catalog JSON.

Pure Python (no lxml, no subprocess): ``config.py``, the typography plan validator and the routes
import it. The catalog *content* (which families, which twins) lives in ``font_catalog.py``
(B-FONTS); this module only defines the shapes and the validation rules every layer shares.

Units: a font size is in points (pt) in JSON and in Settings, a multiple of 0.5 between
``SIZE_MIN`` and ``SIZE_MAX``. OOXML stores half-points (``w:sz w:val="22"`` is 11 pt); use
:func:`half_points`. See docs/EXPORT_DESIGN.md (b), (d) and (f).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from . import export_json as jc
from .export_errors import ExportInputError

MAX_FAMILY_CHARS = 100
FORBIDDEN_IN_FAMILY = frozenset(":,\\=\"';<>{}()[]")  # fontconfig + CSS syntax
SIZE_MIN = 6.0
SIZE_MAX = 72.0
SIZE_STEP = 0.5
SIZE_PRESETS: tuple[float, ...] = (8, 9, 10, 10.5, 11, 12, 14, 16, 18, 20, 24, 28, 32, 36)
MAX_CATALOG = 120
MAX_CSS_CHARS = 300
MAX_LICENCE_CHARS = 60
MAX_SOURCE_CHARS = 60
MAX_TWINS = 6

CATEGORIES = ("sans", "serif", "mono", "display")
ROLES = ("body", "heading", "mono")
STATUSES = ("installed", "substituted", "missing")
GENERIC_FAMILIES = frozenset(
    ("serif", "sans-serif", "monospace", "system-ui", "cursive", "fantasy", "ui-monospace")
)
CSS_BARE = re.compile(r"[A-Za-z][A-Za-z0-9 -]{0,63}")
CSS_QUOTED = re.compile(r'"([A-Za-z0-9][A-Za-z0-9 ._-]{0,99})"')
SOURCE = re.compile(r"(?:apt:fonts-[a-z0-9-]{1,50}|download:[a-z0-9-]{1,40}|system|none)")
LICENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .+/-]{0,59}")
PDF_SUBSET = re.compile(r"^[A-Z]{6}\+")
PDF_STYLE_SUFFIX = re.compile(
    r"[-,_](?:bold|italic|oblique|regular|roman|semibold|medium|black|light)+$", re.IGNORECASE
)
PDF_VENDOR_SUFFIX = re.compile(r"(?:PSMT|PS|MT)$")


def valid_family(family: object) -> bool:
    """A family name safe for ``fc-match``, for OOXML attributes and for a quoted CSS stack: no
    leading '-', no fontconfig or CSS syntax, printable, 1-100 characters after trimming."""
    if not isinstance(family, str):
        return False
    name = family.strip()
    if not name or len(name) > MAX_FAMILY_CHARS or name.startswith("-"):
        return False
    if any(ch in FORBIDDEN_IN_FAMILY for ch in name):
        return False
    return all(ch.isprintable() for ch in name)


def clean_family(value: object, path: str = "family") -> str:
    """A user-typed family: trimmed, inner whitespace collapsed. ``""`` means "keep the
    document's font". Raises :class:`ExportInputError` (``font_name``) otherwise."""
    if not isinstance(value, str):
        raise ExportInputError("font_name", field=path)
    name = " ".join(value.split())
    if not name:
        return ""
    if not valid_family(name):
        raise ExportInputError("font_name", field=path)
    return name


def parse_size(value: object, path: str = "size") -> float:
    """A strict size from a plan: an int or float, a multiple of 0.5 in [6, 72]."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExportInputError("font_size", field=path)
    size = float(value)
    if not math.isfinite(size) or not SIZE_MIN <= size <= SIZE_MAX:
        raise ExportInputError("font_size", field=path)
    if size * 2 != int(size * 2):
        raise ExportInputError("font_size", field=path)
    return size


def clamp_size(value: object) -> float:
    """A lenient size for Settings and env overrides: numbers and numeric strings are clamped to
    [6, 72] and rounded to the nearest 0.5; ``0``, a negative number or ``""`` means off
    (``0.0``). A bool, NaN, infinity or non-number raises ``ValueError``."""
    if isinstance(value, bool):
        raise ValueError("font size must be a number")
    if isinstance(value, str) and not value.strip():
        return 0.0
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("font size must be a number") from exc
    if not math.isfinite(number):
        raise ValueError("font size must be a number")
    if number <= 0:
        return 0.0
    return max(SIZE_MIN, min(SIZE_MAX, math.floor(number * 2 + 0.5) / 2))


def half_points(size: float) -> int:
    """Points to the OOXML ``w:sz``/``w:szCs`` value (half-points)."""
    return int(round(size * 2))


def squash(name: str) -> str:
    """Lookup key for a family: lower-case letters and digits only (``"Times New Roman"`` and
    ``"TimesNewRoman"`` both give ``"timesnewroman"``)."""
    return "".join(ch for ch in name.casefold() if ch.isalnum())


def strip_pdf_font_name(raw: str) -> str:
    """A PDF/pdf2docx font name reduced to its family part: subset prefix ``ABCDEF+`` removed,
    ``,Bold``/``-Italic`` style suffixes and ``PSMT``/``MT`` vendor suffixes removed. The result
    is still unvalidated: look it up with :func:`squash` in the catalog."""
    name = PDF_SUBSET.sub("", raw.strip())[:MAX_FAMILY_CHARS]
    name = name.split(",", 1)[0]
    name = PDF_VENDOR_SUFFIX.sub("", name)
    name = PDF_STYLE_SUFFIX.sub("", name)
    return PDF_VENDOR_SUFFIX.sub("", name).rstrip("-, ")


def valid_css_stack(stack: object) -> bool:
    """A CSS ``font-family`` list made only of double-quoted family names and bare identifiers."""
    if not isinstance(stack, str) or not stack or len(stack) > MAX_CSS_CHARS:
        return False
    for token in (part.strip() for part in stack.split(",")):
        if not (CSS_QUOTED.fullmatch(token) or CSS_BARE.fullmatch(token)):
            return False
    return True


def custom_css(family: str, category: str = "sans") -> str:
    """The CSS stack for a free-text family: the quoted name, then a generic fallback."""
    generic = {"serif": "serif", "mono": "monospace"}.get(category, "sans-serif")
    name = clean_family(family)
    return f'"{name}", {generic}' if name else generic


def _family(value: Any, path: str) -> str:
    name = clean_family(value, path)
    if not name:
        raise ExportInputError("font_name", field=path)
    return name


@dataclass(frozen=True)
class FontEntry:
    """One static catalog row (font_catalog.CATALOG). ``twins`` is the renderer's fallback chain
    for this name, first installed wins (fontconfig ``<accept>`` order). ``metric_twin`` is the
    twin that keeps line and page breaks identical ("" when none exists)."""

    family: str
    category: str
    roles: tuple[str, ...]
    css: str
    proprietary: bool
    licence: str
    source: str
    twins: tuple[str, ...] = ()
    metric_twin: str = ""

    def __post_init__(self) -> None:
        _entry_fields(self.to_json(), "font")

    def to_json(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "category": self.category,
            "roles": list(self.roles),
            "css": self.css,
            "proprietary": self.proprietary,
            "licence": self.licence,
            "source": self.source,
            "twins": list(self.twins),
            "metric_twin": self.metric_twin,
        }

    @classmethod
    def from_json(cls, value: Any, path: str = "font") -> FontEntry:
        return cls(**_entry_fields(value, path))


def _entry_fields(value: Any, path: str) -> dict[str, Any]:
    keys = ("family", "category", "roles", "css", "proprietary", "licence", "source")
    data = jc.record(value, path, keys, ("twins", "metric_twin"))
    if not valid_css_stack(data["css"]):
        raise jc.fail(f"{path}.css")
    twins = jc.items(data.get("twins", []), f"{path}.twins", MAX_TWINS, _family)
    metric = clean_family(data.get("metric_twin", ""), f"{path}.metric_twin")
    if metric and metric not in twins:
        raise jc.fail(f"{path}.metric_twin")
    return {
        "family": _family(data["family"], f"{path}.family"),
        "category": jc.choice(data["category"], f"{path}.category", CATEGORIES),
        "roles": _roles(data["roles"], f"{path}.roles"),
        "css": data["css"],
        "proprietary": jc.flag(data["proprietary"], f"{path}.proprietary"),
        "licence": jc.required(data["licence"], f"{path}.licence", MAX_LICENCE_CHARS, LICENCE),
        "source": jc.required(data["source"], f"{path}.source", MAX_SOURCE_CHARS, SOURCE),
        "twins": twins,
        "metric_twin": metric,
    }


def _roles(value: Any, path: str) -> tuple[str, ...]:
    roles = jc.items(value, path, len(ROLES), lambda item, at: jc.choice(item, at, ROLES))
    if not roles:
        raise jc.fail(path)
    jc.unique(roles, path)
    return roles


@dataclass(frozen=True)
class FontStatus:
    """One row of ``GET /fonts``: a catalog entry (or a probed free-text name, ``custom``) with
    what the PDF renderer will really use. ``resolved`` is the family fontconfig picks under the
    renderer's config; ``layout_safe`` is true when it is the family itself or its metric twin.
    Never carries a host path or an install command (those are admin-only, in /health)."""

    family: str
    category: str
    roles: tuple[str, ...]
    css: str
    proprietary: bool
    licence: str
    status: str
    resolved: str
    layout_safe: bool
    custom: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "category": self.category,
            "roles": list(self.roles),
            "css": self.css,
            "proprietary": self.proprietary,
            "licence": self.licence,
            "status": self.status,
            "resolved": self.resolved,
            "layout_safe": self.layout_safe,
            "custom": self.custom,
        }

    @classmethod
    def from_json(cls, value: Any, path: str = "font") -> FontStatus:
        keys = ("family", "category", "roles", "css", "proprietary", "licence", "status")
        data = jc.record(value, path, (*keys, "resolved", "layout_safe", "custom"))
        if not valid_css_stack(data["css"]):
            raise jc.fail(f"{path}.css")
        custom = jc.flag(data["custom"], f"{path}.custom")
        licence = jc.text(data["licence"], f"{path}.licence", MAX_LICENCE_CHARS, pattern=LICENCE)
        if not licence and not custom:
            raise jc.fail(f"{path}.licence")
        status = jc.choice(data["status"], f"{path}.status", STATUSES)
        resolved = clean_family(data["resolved"], f"{path}.resolved")
        if status != "installed" and not resolved:
            raise jc.fail(f"{path}.resolved")
        return cls(
            family=_family(data["family"], f"{path}.family"),
            category=jc.choice(data["category"], f"{path}.category", CATEGORIES),
            roles=_roles(data["roles"], f"{path}.roles"),
            css=data["css"],
            proprietary=jc.flag(data["proprietary"], f"{path}.proprietary"),
            licence=licence,
            status=status,
            resolved=resolved,
            layout_safe=jc.flag(data["layout_safe"], f"{path}.layout_safe"),
            custom=custom,
        )


@dataclass(frozen=True)
class FontCatalog:
    """The ``GET /__ai/api/docx/export/fonts`` answer. ``checked`` is false when fontconfig could
    not be asked (``fc-list`` missing): every status is then ``missing``/unknown and the UI says
    so instead of claiming fonts are installed."""

    fonts: tuple[FontStatus, ...]
    checked: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "fonts": [font.to_json() for font in self.fonts],
            "sizes": list(SIZE_PRESETS),
            "min_size": SIZE_MIN,
            "max_size": SIZE_MAX,
            "step": SIZE_STEP,
            "checked": self.checked,
        }

    @classmethod
    def from_json(cls, value: Any, path: str = "catalog") -> FontCatalog:
        keys = ("fonts", "sizes", "min_size", "max_size", "step", "checked")
        data = jc.record(value, path, keys)
        fonts = jc.items(data["fonts"], f"{path}.fonts", MAX_CATALOG, FontStatus.from_json)
        jc.unique((squash(font.family) for font in fonts), f"{path}.fonts")
        jc.items(data["sizes"], f"{path}.sizes", 40, parse_size)
        for key in ("min_size", "max_size", "step"):
            jc.number(data[key], f"{path}.{key}", 0.5, SIZE_MAX)
        return cls(fonts=fonts, checked=jc.flag(data["checked"], f"{path}.checked"))

    def find(self, family: str) -> FontStatus | None:
        key = squash(family)
        return next((font for font in self.fonts if squash(font.family) == key), None)


__all__ = [
    "CATEGORIES",
    "FORBIDDEN_IN_FAMILY",
    "MAX_FAMILY_CHARS",
    "ROLES",
    "SIZE_MAX",
    "SIZE_MIN",
    "SIZE_PRESETS",
    "SIZE_STEP",
    "STATUSES",
    "FontCatalog",
    "FontEntry",
    "FontStatus",
    "clamp_size",
    "clean_family",
    "custom_css",
    "half_points",
    "parse_size",
    "squash",
    "strip_pdf_font_name",
    "valid_css_stack",
    "valid_family",
]
