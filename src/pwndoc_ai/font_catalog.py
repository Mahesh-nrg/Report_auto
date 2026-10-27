"""The curated font catalog: which families the UI offers and what the PDF renderer really uses.

``CATALOG`` lists common Word fonts (proprietary, never installed here) and the open families
``scripts/install_fonts.sh`` installs. ``ALIAS_CHAINS`` names, for every proprietary family, the
open candidates the renderer's fontconfig accepts in order (first installed wins);
``METRIC_TWINS`` names the candidates that keep line and page breaks identical.

``catalog_status`` is pure: it turns an installed set and a ``family -> resolved`` lookup into
the ``GET /fonts`` answer. ``font_catalog`` asks fontconfig itself, under the same generated
configuration the LibreOffice renderer uses (system fonts plus the project fonts directory),
with a deadline and a TTL cache. Names reach ``fc-match`` only after ``valid_family``.
See docs/EXPORT_DESIGN.md (b) and (f) and docs/FONTS.md.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from .font_catalog_model import (
    FontCatalog,
    FontEntry,
    FontStatus,
    custom_css,
    squash,
    valid_family,
)

FONTS_DIR_ENV = "PWNDOC_AI_FONTS_DIR"
DATA_DIR_ENV = "PWNDOC_AI_DATA_DIR"
FONTS_DIR_NAME = "fonts"
CATALOG_DEADLINE = 15.0
DEFAULT_TTL = 300.0
GENERIC = {"sans": "sans-serif", "serif": "serif", "mono": "monospace", "display": "sans-serif"}
MS = "Proprietary Microsoft"
OFL = "OFL-1.1"
APACHE = "Apache-2.0"
BODY_HEADING = ("body", "heading")

SANS = ("Carlito", "Liberation Sans")
SERIF = ("Caladea", "Liberation Serif")
ALIAS_CHAINS: dict[str, tuple[str, ...]] = {
    "Calibri": SANS,
    "Calibri Light": SANS,
    "Aptos": SANS,
    "Aptos Display": SANS,
    "Aptos Narrow": SANS,
    "Cambria": SERIF,
    "Segoe UI": ("Selawik", "Open Sans", "Noto Sans", "Liberation Sans"),
    "Verdana": ("DejaVu Sans", "Liberation Sans"),
    "Tahoma": ("DejaVu Sans", "Liberation Sans"),
    "Georgia": ("Gelasio", "Noto Serif", "Liberation Serif"),
    "Consolas": ("Inconsolata", "Liberation Mono"),
    "Garamond": ("EB Garamond", "Liberation Serif"),
    "Century Gothic": ("URW Gothic", "Liberation Sans"),
    "Palatino Linotype": ("P052", "Liberation Serif"),
    "Book Antiqua": ("P052", "Liberation Serif"),
    "Trebuchet MS": ("Open Sans", "Liberation Sans"),
    "Arial": ("Liberation Sans", "Arimo"),
    "Helvetica": ("Liberation Sans", "Arimo"),
    "Times New Roman": ("Liberation Serif", "Tinos"),
    "Courier New": ("Liberation Mono", "Cousine"),
}
METRIC_TWINS: dict[str, frozenset[str]] = {
    **dict.fromkeys(("calibri", "calibri light"), frozenset({"Carlito"})),
    "cambria": frozenset({"Caladea"}),
    **dict.fromkeys(("arial", "helvetica"), frozenset({"Liberation Sans", "Arimo"})),
    "times new roman": frozenset({"Liberation Serif", "Tinos"}),
    "courier new": frozenset({"Liberation Mono", "Cousine"}),
    "segoe ui": frozenset({"Selawik"}),
    "georgia": frozenset({"Gelasio"}),
    **dict.fromkeys(("palatino linotype", "book antiqua"), frozenset({"P052"})),
}


def _css(family: str, *extra: str, category: str = "sans") -> str:
    names = ", ".join(f'"{name}"' for name in (family, *extra))
    return f"{names}, {GENERIC[category]}"


def _proprietary(family: str, category: str, roles: tuple[str, ...]) -> FontEntry:
    chain = ALIAS_CHAINS[family]
    twins = METRIC_TWINS.get(family.casefold(), frozenset())
    metric = next((name for name in chain if name in twins), "")
    shown = tuple(name for name in chain if name != "DejaVu Sans")
    return FontEntry(
        family=family,
        category=category,
        roles=roles,
        css=_css(family, *shown, category=category),
        proprietary=True,
        licence=MS,
        source="none",
        twins=chain,
        metric_twin=metric,
    )


def _open(
    family: str, category: str, roles: tuple[str, ...], licence: str, source: str
) -> FontEntry:
    return FontEntry(
        family=family,
        category=category,
        roles=roles,
        css=_css(family, category=category),
        proprietary=False,
        licence=licence,
        source=source,
    )


PROPRIETARY_ROWS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Calibri", "sans", BODY_HEADING),
    ("Calibri Light", "sans", ("heading",)),
    ("Aptos", "sans", BODY_HEADING),
    ("Cambria", "serif", BODY_HEADING),
    ("Arial", "sans", BODY_HEADING),
    ("Times New Roman", "serif", BODY_HEADING),
    ("Courier New", "mono", ("mono",)),
    ("Segoe UI", "sans", BODY_HEADING),
    ("Verdana", "sans", BODY_HEADING),
    ("Tahoma", "sans", BODY_HEADING),
    ("Georgia", "serif", BODY_HEADING),
    ("Consolas", "mono", ("mono",)),
    ("Palatino Linotype", "serif", BODY_HEADING),
    ("Century Gothic", "display", ("heading",)),
    ("Garamond", "serif", BODY_HEADING),
    ("Trebuchet MS", "sans", BODY_HEADING),
)
OPEN_ROWS: tuple[tuple[str, str, tuple[str, ...], str, str], ...] = (
    ("Carlito", "sans", BODY_HEADING, OFL, "apt:fonts-crosextra-carlito"),
    ("Caladea", "serif", BODY_HEADING, APACHE, "apt:fonts-crosextra-caladea"),
    ("Liberation Sans", "sans", BODY_HEADING, OFL, "apt:fonts-liberation"),
    ("Liberation Serif", "serif", BODY_HEADING, OFL, "apt:fonts-liberation"),
    ("Liberation Mono", "mono", ("mono",), OFL, "apt:fonts-liberation"),
    ("Lexend", "sans", BODY_HEADING, OFL, "download:lexend"),
    ("Lexend Deca", "sans", BODY_HEADING, OFL, "download:lexend-deca"),
    ("Open Sans", "sans", BODY_HEADING, APACHE, "apt:fonts-open-sans"),
    ("Roboto", "sans", BODY_HEADING, APACHE, "apt:fonts-roboto-unhinted"),
    ("Lato", "sans", BODY_HEADING, OFL, "apt:fonts-lato"),
    ("Inter", "sans", BODY_HEADING, OFL, "apt:fonts-inter"),
    ("Noto Sans", "sans", BODY_HEADING, OFL, "apt:fonts-noto-core"),
    ("Noto Serif", "serif", BODY_HEADING, OFL, "apt:fonts-noto-core"),
    ("Source Sans 3", "sans", BODY_HEADING, OFL, "apt:fonts-adobe-sourcesans3"),
    ("Ubuntu", "sans", BODY_HEADING, "Ubuntu Font Licence 1.0", "apt:fonts-ubuntu"),
    ("IBM Plex Sans", "sans", BODY_HEADING, OFL, "apt:fonts-ibm-plex"),
    ("DejaVu Sans", "sans", BODY_HEADING, "Bitstream Vera", "apt:fonts-dejavu-core"),
    ("DejaVu Serif", "serif", BODY_HEADING, "Bitstream Vera", "apt:fonts-dejavu-core"),
    ("DejaVu Sans Mono", "mono", ("mono",), "Bitstream Vera", "apt:fonts-dejavu-core"),
    ("EB Garamond", "serif", BODY_HEADING, OFL, "apt:fonts-ebgaramond"),
    ("Gelasio", "serif", BODY_HEADING, OFL, "download:gelasio"),
    ("Fira Code", "mono", ("mono",), OFL, "apt:fonts-firacode"),
    ("JetBrains Mono", "mono", ("mono",), OFL, "apt:fonts-jetbrains-mono"),
    ("Inconsolata", "mono", ("mono",), OFL, "apt:fonts-inconsolata"),
)
CATALOG: tuple[FontEntry, ...] = (
    *(_proprietary(*row) for row in PROPRIETARY_ROWS),
    *(_open(*row) for row in OPEN_ROWS),
)


def fonts_dir(data_dir: Path, env: Mapping[str, str] = os.environ) -> Path:
    """The project-local fonts directory: ``$PWNDOC_AI_FONTS_DIR`` or ``<data_dir>/fonts``."""
    override = env.get(FONTS_DIR_ENV, "").strip()
    return Path(override) if override else Path(data_dir) / FONTS_DIR_NAME


def default_fonts_dir(env: Mapping[str, str] = os.environ) -> Path | None:
    """The renderer's fonts directory when no data_dir is at hand (gateway env), if it exists."""
    data_dir = Path(env.get(DATA_DIR_ENV, "").strip() or "data")
    path = fonts_dir(data_dir.resolve(), env)
    return path if path.is_dir() else None


# -- status -------------------------------------------------------------------------------------
def _twin_ok(family: str, resolved: str) -> bool:
    twins = METRIC_TWINS.get(family.casefold(), frozenset())
    return squash(resolved) in {squash(name) for name in twins}


def _status(entry: FontEntry, present: frozenset[str], resolved: str | None) -> FontStatus:
    base = {
        "family": entry.family,
        "category": entry.category,
        "roles": entry.roles,
        "css": entry.css,
        "proprietary": entry.proprietary,
        "licence": entry.licence,
    }
    if squash(entry.family) in present:
        return FontStatus(**base, status="installed", resolved=entry.family, layout_safe=True)
    if resolved and valid_family(resolved) and squash(resolved) == squash(entry.family):
        return FontStatus(**base, status="installed", resolved=entry.family, layout_safe=True)
    if resolved and valid_family(resolved):
        safe = _twin_ok(entry.family, resolved)
        return FontStatus(**base, status="substituted", resolved=resolved, layout_safe=safe)
    fallback = GENERIC[entry.category]
    return FontStatus(**base, status="missing", resolved=fallback, layout_safe=False)


def catalog_status(
    installed: frozenset[str],
    resolve: Callable[[str], str | None],
    *,
    checked: bool = True,
) -> FontCatalog:
    """The catalog with each family's status. Pure: fontconfig is behind ``resolve``.

    ``installed`` when the family is in ``installed``; ``substituted`` when ``resolve`` names
    another family (``layout_safe`` when it is a metric twin); ``missing`` when it names
    nothing, or when ``checked`` is false (fontconfig could not be asked at all).
    """
    if not checked:
        return FontCatalog(
            fonts=tuple(_status(entry, frozenset(), None) for entry in CATALOG), checked=False
        )
    present = frozenset(squash(name) for name in installed)
    rows = []
    for entry in CATALOG:
        answer = None if squash(entry.family) in present else resolve(entry.family)
        rows.append(_status(entry, present, answer))
    return FontCatalog(fonts=tuple(rows), checked=True)


def status_for(family: str, catalog: FontCatalog) -> FontStatus:
    """The catalog row for ``family``, or a ``custom`` row for a free-text "Other..." name.

    A custom name is reported as installed only when it is in no catalog row and nothing is
    known about it: the caller probes it with fontconfig if it needs to know (``probe_custom``).
    """
    found = catalog.find(family)
    if found is not None:
        return found
    return FontStatus(
        family=family.strip(),
        category="sans",
        roles=("body", "heading"),
        css=custom_css(family),
        proprietary=False,
        licence="",
        status="missing",
        resolved="sans-serif",
        layout_safe=False,
        custom=True,
    )


def probe_custom(
    family: str, catalog: FontCatalog, resolve: Callable[[str], str | None]
) -> FontStatus:
    """:func:`status_for` plus a fontconfig answer for a custom name (installed/substituted)."""
    row = status_for(family, catalog)
    if not row.custom or not valid_family(family):
        return row
    answer = resolve(row.family)
    if not answer or not valid_family(answer):
        return row
    same = squash(answer) == squash(row.family)
    return FontStatus(
        family=row.family,
        category=row.category,
        roles=row.roles,
        css=row.css,
        proprietary=False,
        licence="",
        status="installed" if same else "substituted",
        resolved=row.family if same else answer,
        layout_safe=same,
        custom=True,
    )


# -- fontconfig, cached -------------------------------------------------------------------------
_lock = threading.Lock()
_cache: dict[str, tuple[float, FontCatalog]] = {}


def clear_caches() -> None:
    """Forget every cached catalog (call after installing fonts, with
    ``pdf_fonts.installed_families.cache_clear()``)."""
    with _lock:
        _cache.clear()


def dir_families(directory: Path | None) -> frozenset[str]:
    """Families in ``directory`` alone (no system fonts), through ``fc-list``."""
    from . import pdf_fonts

    if directory is None or not directory.is_dir():
        return frozenset()
    with tempfile.TemporaryDirectory(prefix="pwndoc-fonts-") as work:
        conf = Path(work) / pdf_fonts.FONTS_CONF_NAME
        conf.write_text(pdf_fonts.dir_only_fontconfig(directory, Path(work)), encoding="utf-8")
        return pdf_fonts.installed_families(conf)


def _probe(directory: Path | None) -> FontCatalog:
    from . import pdf_fonts

    if shutil.which("fc-list") is None or shutil.which("fc-match") is None:
        return catalog_status(frozenset(), lambda family: None, checked=False)
    deadline = time.monotonic() + CATALOG_DEADLINE
    with tempfile.TemporaryDirectory(prefix="pwndoc-fonts-") as work:
        base = pdf_fonts.write_fontconfig(Path(work), frozenset(), fonts_dir=directory)
        installed = pdf_fonts.installed_families(base)
        conf = pdf_fonts.write_fontconfig(Path(work), installed, fonts_dir=directory)
        matcher = pdf_fonts.fc_matcher(conf, deadline)
        return catalog_status(installed, matcher, checked=bool(installed))


def font_catalog(data_dir: Path, *, ttl: float = DEFAULT_TTL) -> FontCatalog:
    """The catalog as the renderer sees it (system fonts plus the project fonts directory),
    cached for ``ttl`` seconds per fonts directory."""
    directory = fonts_dir(Path(data_dir))
    key = str(directory)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached is not None and now - cached[0] < ttl:
            return cached[1]
    result = _probe(directory if directory.is_dir() else None)
    with _lock:
        _cache[key] = (time.monotonic(), result)
    return result


__all__ = [
    "ALIAS_CHAINS",
    "CATALOG",
    "METRIC_TWINS",
    "catalog_status",
    "clear_caches",
    "default_fonts_dir",
    "dir_families",
    "font_catalog",
    "fonts_dir",
    "probe_custom",
    "status_for",
]
