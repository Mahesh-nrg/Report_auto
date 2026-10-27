"""Requested fonts and what the renderer substitutes for them.

The families come from the runs of the body, headers and footers, the styles in use (with their
ancestors and the document defaults), the theme (``minorHAnsi`` and friends) and the numbering
levels in use. Each family is resolved through fontconfig (``fc-match``, no shell, a 5 second
timeout, cached per process) unless the caller injects a resolver. A substitute keeps the layout
when it is the family itself or a known metric-compatible font (Carlito for Calibri, ...).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable

from lxml import etree

from . import budget
from .load import LoadedDoc, read_xml
from .model import FontIssue
from .ooxml import w

log = logging.getLogger(__name__)

Resolver = Callable[[str], "str | None"]

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
FC_TIMEOUT = 5.0
MAX_FAMILY_LENGTH = 100
MAX_FAMILIES = 50
MAX_CACHED = 256  # families remembered per process (least recently used go first)
MAX_OUTPUT = 200
FONT_ATTRS = ("ascii", "hAnsi", "asciiTheme", "hAnsiTheme")
STYLE_REFS = ("pStyle", "rStyle", "tblStyle")
FORBIDDEN = frozenset(":,\\=\"'")
SYMBOL_FONTS = frozenset(
    {"symbol", "wingdings", "wingdings 2", "wingdings 3", "webdings", "marlett", "mt extra"}
    | {"zapf dingbats", "zapfdingbats", "ms outlook", "bookshelf symbol 7"}
)
METRIC_PAIRS = frozenset(
    {
        ("calibri", "carlito"),
        ("cambria", "caladea"),
        ("arial", "liberation sans"),
        ("arial", "arimo"),
        ("helvetica", "liberation sans"),
        ("helvetica", "arimo"),
        ("times new roman", "liberation serif"),
        ("times new roman", "tinos"),
        ("courier new", "liberation mono"),
        ("courier new", "cousine"),
    }
)
PACKAGES = {
    "calibri": "fonts-crosextra-carlito",
    "calibri light": "fonts-crosextra-carlito",
    "aptos": "fonts-crosextra-carlito",
    "aptos display": "fonts-crosextra-carlito",
    "aptos narrow": "fonts-crosextra-carlito",
    "cambria": "fonts-crosextra-caladea",
    "cambria math": "fonts-crosextra-caladea",
    "arial": "fonts-liberation2",
    "helvetica": "fonts-liberation2",
    "times new roman": "fonts-liberation2",
    "courier new": "fonts-liberation2",
}

_cache: OrderedDict[str, str | None] = OrderedDict()
_lock = threading.Lock()


# -- resolution ------------------------------------------------------------------------------
def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _valid_family(family: object) -> bool:
    if not isinstance(family, str) or not family.strip() or len(family) > MAX_FAMILY_LENGTH:
        return False
    if family.startswith("-") or any(ch in FORBIDDEN for ch in family):
        return False
    return all(ch.isprintable() for ch in family)


def _run_fc_match(family: str) -> str | None:
    binary = shutil.which("fc-match")
    if binary is None:
        return None
    pattern = family.replace("-", "\\-")
    left = budget.remaining()
    timeout = FC_TIMEOUT if left is None else min(FC_TIMEOUT, left)
    if timeout <= 0:
        return None
    try:
        done = subprocess.run(  # noqa: S603 - fixed binary, argument list, no shell
            [binary, "-f", "%{family[0]}", "--", pattern],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("font check: fc-match failed (%s)", type(exc).__name__)
        return None
    if done.returncode != 0:
        log.warning("font check: fc-match exited with status %s", done.returncode)
        return None
    resolved = done.stdout.decode("utf-8", "replace").strip()[:MAX_OUTPUT]
    return resolved or None


def fc_match(family: str) -> str | None:
    """The family fontconfig renders for ``family``; None when that cannot be determined."""
    if not _valid_family(family):
        return None
    with _lock:
        if family in _cache:
            _cache.move_to_end(family)
            return _cache[family]
    resolved = _run_fc_match(family)
    with _lock:
        _cache[family] = resolved
        _cache.move_to_end(family)
        while len(_cache) > MAX_CACHED:
            _cache.popitem(last=False)
    return resolved


CLOSE_SUBSTITUTES = frozenset({"carlito", "caladea"})  # the packages' own fonts, already installed


def compatible(family: str, resolved: str) -> bool:
    wanted, got = family.strip().casefold(), resolved.strip().casefold()
    return wanted == got or (wanted, got) in METRIC_PAIRS


def _advice(family: str, resolved: str | None, ok: bool) -> str:
    package = PACKAGES.get(family.casefold())
    if resolved is None:
        return "The substitute font could not be determined (is fontconfig installed?)."
    if ok:
        return "Installed." if family.casefold() == resolved.casefold() else "Metric-compatible."
    if resolved.casefold() in CLOSE_SUBSTITUTES:
        return f"Shown in {resolved}, which is close but not identical; line breaks may differ."
    if family.casefold().startswith("aptos"):
        return f"No metric-compatible free font exists; install {package} for a close match."
    if package:
        return f"Install {package} to keep the line and page breaks of Word."
    return "Install the font on the server to keep the line and page breaks of Word."


def font_issue(family: str, resolver: Resolver) -> FontIssue:
    try:
        # A name from the document never reaches a resolver unchecked (it could be an option).
        resolved = resolver(family) if _valid_family(family) else None
    except Exception as exc:  # noqa: BLE001 - an injected resolver must not break the check
        log.warning("font check: the resolver failed (%s)", type(exc).__name__)
        resolved = None
    resolved = resolved.strip()[:MAX_OUTPUT] if isinstance(resolved, str) else None
    ok = bool(resolved) and compatible(family, resolved or "")
    return FontIssue(family, resolved or "", ok, _advice(family, resolved or None, ok))


# -- discovery -------------------------------------------------------------------------------
def theme_fonts(theme: etree._Element | None) -> dict[str, str]:
    """{"major": family, "minor": family} from a theme part (latin typefaces)."""
    if theme is None:
        return {}
    found: dict[str, str] = {}
    for kind in ("major", "minor"):
        latin = theme.find(f".//{{{A_NS}}}{kind}Font/{{{A_NS}}}latin")
        face = latin.get("typeface") if latin is not None else None
        if face:
            found[kind] = face
    return found


def _rfont_values(root: etree._Element) -> list[str]:
    values: list[str] = []
    for fonts in root.iter(w("rFonts")):
        values.extend(v for name in FONT_ATTRS if (v := fonts.get(w(name))))
    return values


def _style_ids(roots: Iterable[etree._Element]) -> set[str]:
    ids: set[str] = set()
    for root in roots:
        for tag in STYLE_REFS:
            ids.update(v for node in root.iter(w(tag)) if (v := node.get(w("val"))))
    return ids


def _numbering_values(doc: LoadedDoc, roots: list[etree._Element]) -> list[str]:
    numbering = doc.part_roots("numbering")
    if not numbering:
        return []
    used = {v for root in roots for n in root.iter(w("numId")) if (v := n.get(w("val")))}
    abstract = {
        num.get(w("numId")): node.get(w("val"))
        for num in numbering[0].findall(w("num"))
        if (node := num.find(w("abstractNumId"))) is not None
    }
    wanted = {abstract.get(num_id) for num_id in used} - {None}
    values: list[str] = []
    for item in numbering[0].findall(w("abstractNum")):
        if item.get(w("abstractNumId")) in wanted:
            values.extend(_rfont_values(item))
    return values


def _styles_root(doc: LoadedDoc) -> etree._Element | None:
    names = doc.parts.get("styles", ())
    return read_xml(doc.package, names[0]) if names else None


def requested_fonts(doc: LoadedDoc) -> tuple[str, ...]:
    """The latin font families the document asks for, in first-use order, without symbols."""
    roots = [doc.root, *doc.part_roots("header"), *doc.part_roots("footer")]
    styles_root = _styles_root(doc)
    values = [v for root in roots for v in _rfont_values(root)]
    values += doc.styles.used_fonts(_style_ids(roots))
    values += _numbering_values(doc, [*roots, *([styles_root] if styles_root is not None else [])])
    themes = doc.part_roots("theme")
    theme = theme_fonts(themes[0] if themes else None)
    families: dict[str, None] = {}
    for value in values:
        family = theme.get(value[:5]) if value.startswith(("major", "minor")) else value
        family = family.strip() if family else ""
        if family and family.casefold() not in SYMBOL_FONTS:
            families.setdefault(family)
            if len(families) >= MAX_FAMILIES:
                break
    return tuple(families)


def check_fonts(doc: LoadedDoc, resolver: Resolver | None = None) -> tuple[FontIssue, ...]:
    chosen = resolver or fc_match
    return tuple(font_issue(family, chosen) for family in requested_fonts(doc))
