"""Fonts for the LibreOffice conversion: a generated fontconfig file and substitution warnings.

Word reports use Calibri, Cambria and (newer) the Aptos theme font. None is installed on a Linux
host, and LibreOffice then falls back to DejaVu Sans, about 15 percent wider, which moves every
line break and page break. The open Carlito (Calibri) and Caladea (Cambria) fonts have the same
metrics. ``write_fontconfig`` writes a fontconfig file into the per-conversion work directory
(``FONTCONFIG_FILE``) that includes the system configuration and adds alias rules, so the render
uses the metric-compatible font when it is installed and Liberation otherwise, never DejaVu.
Only families that ``fc-list`` reports are named in a rule, so nothing can fail on a missing font.

``font_warnings`` tells which fonts a document asks for that will be substituted by a font that
does not keep the layout, naming the Debian packages that fix it.
"""

from __future__ import annotations

import functools
import io
import logging
import os
import re
import shutil
import subprocess
import time
import zipfile
from collections.abc import Callable, Collection, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from .font_catalog import ALIAS_CHAINS, default_fonts_dir

log = logging.getLogger(__name__)

try:
    from .docx_layout import FontIssue
except ImportError:  # the layout package is optional for the renderer

    @dataclass(frozen=True)
    class FontIssue:  # type: ignore[no-redef]
        font: str
        resolved: str
        compatible: bool
        advice: str


FONTS_CONF_NAME = "fonts.conf"
SYSTEM_FONTS_CONF = "/etc/fonts/fonts.conf"
FC_TIMEOUT = 5.0
MAX_FAMILIES = 50
MAX_PART_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024  # all parts together: later ones are not read
MAX_MEMBERS = 4000
MAX_FAMILY_LENGTH = 100
FORBIDDEN_IN_FAMILY = frozenset(":,\\=\"'")  # fontconfig pattern syntax
DEFAULT_RESOLVED = "the default font"
CALIBRI_PACKAGE, CAMBRIA_PACKAGE = "fonts-crosextra-carlito", "fonts-crosextra-caladea"

SANS_CHAIN = ("Carlito", "Liberation Sans")
SERIF_CHAIN = ("Caladea", "Liberation Serif")
FALLBACK_CHAINS: dict[str, tuple[str, ...]] = {
    "Calibri": SANS_CHAIN,
    "Calibri Light": SANS_CHAIN,
    "Aptos": SANS_CHAIN,
    "Aptos Display": SANS_CHAIN,
    "Aptos Narrow": SANS_CHAIN,
    "Cambria": SERIF_CHAIN,
    "Cambria Math": SERIF_CHAIN,
    "Segoe UI": ("Liberation Sans",),
    "Tahoma": ("Liberation Sans",),
    "Verdana": ("Liberation Sans",),
    "Microsoft Sans Serif": ("Liberation Sans",),
    "Arial": ("Liberation Sans",),
    "Helvetica": ("Liberation Sans",),
    "Times New Roman": ("Liberation Serif",),
    "Consolas": ("Liberation Mono",),
    "Courier New": ("Liberation Mono",),
}
# The catalog's chains (font_catalog.ALIAS_CHAINS). DejaVu is left out: fontconfig falls back to
# it anyway, and the DejaVu rule below puts Liberation in front of it on purpose.
FALLBACK_CHAINS.update(
    (family, tuple(name for name in chain if not name.startswith("DejaVu")))
    for family, chain in ALIAS_CHAINS.items()
)
# Which substitute keeps the metrics of the requested family (lower-case keys).
METRIC_COMPATIBLE: dict[str, frozenset[str]] = {
    **dict.fromkeys(
        ("calibri", "calibri light", "aptos", "aptos display", "aptos narrow"),
        frozenset({"Carlito"}),
    ),
    **dict.fromkeys(("cambria", "cambria math"), frozenset({"Caladea"})),
    **dict.fromkeys(("arial", "helvetica"), frozenset({"Liberation Sans"})),
    "times new roman": frozenset({"Liberation Serif"}),
    "courier new": frozenset({"Liberation Mono"}),
}
# fontconfig's own list puts these before everything else; Liberation is put in front of them.
DEJAVU_TO_LIBERATION = (
    ("DejaVu Sans", "Liberation Sans"),
    ("DejaVu Serif", "Liberation Serif"),
    ("DejaVu Sans Mono", "Liberation Mono"),
    # fonts-noto-core puts Noto ahead of DejaVu in the system's generic preferences
    ("Noto Serif", "Liberation Serif"),
    ("Noto Sans", "Liberation Sans"),
    ("Noto Sans Mono", "Liberation Mono"),
)
IGNORED_FONTS = frozenset(
    {"symbol", "wingdings", "wingdings 2", "wingdings 3", "webdings", "marlett", "sans-serif",
     "serif", "monospace"}
)  # fmt: skip

RFONT_NAME = re.compile(r'\b(?:ascii|hAnsi)="([^"]{1,64})"')
RFONT_THEME = re.compile(r'\b(?:ascii|hAnsi)Theme="(?:major|minor)')
THEME_LATIN = re.compile(r'<a:(?:major|minor)Font>\s*<a:latin\s+typeface="([^"]{1,64})"')
PART_NAME = re.compile(r"^word/(?:[^/]+\.xml|theme/[^/]+\.xml)$")
SKIPPED_PARTS = frozenset({"word/fontTable.xml", "word/settings.xml", "word/webSettings.xml"})
UNESCAPED_COMMA = re.compile(r"(?<!\\),")


# -- installed fonts ------------------------------
def _unescape(name: str) -> str:
    return re.sub(r"\\(.)", r"\1", name).strip()


def _list_families(conf: Path | None) -> frozenset[str]:
    binary = shutil.which("fc-list")
    if not binary:
        log.info("font check: fc-list not found, installed fonts are unknown")
        return frozenset()
    environment = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"}
    if conf is not None:
        environment["FONTCONFIG_FILE"] = str(conf)
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [binary, ":", "family"],
            capture_output=True,
            timeout=FC_TIMEOUT,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("font check: fc-list failed (%s)", type(exc).__name__)
        return frozenset()
    if done.returncode != 0:
        log.info("font check: fc-list exited %s", done.returncode)
        return frozenset()
    names = (
        _unescape(part)
        for line in done.stdout.decode("utf-8", "replace").splitlines()
        for part in UNESCAPED_COMMA.split(line)
    )
    return frozenset(name for name in names if name)


@functools.lru_cache(maxsize=1)
def _ambient_families() -> frozenset[str]:
    return _list_families(None)


def installed_families(conf: Path | None = None) -> frozenset[str]:
    """Font family names that ``fc-list`` reports; empty when it cannot run.

    Without ``conf`` the ambient fontconfig is asked once per process (``cache_clear`` resets
    it); with ``conf`` (a generated file, e.g. one that adds the project fonts directory) it is
    asked every time.
    """
    return _ambient_families() if conf is None else _list_families(conf)


installed_families.cache_clear = _ambient_families.cache_clear  # type: ignore[attr-defined]


# -- the generated configuration ------------------------------
def _alias_rule(family: str, candidates: tuple[str, ...]) -> str:
    accepted = "".join(f"<family>{escape(name)}</family>" for name in candidates)
    return (
        f'  <alias binding="same"><family>{escape(family)}</family>'
        f"<accept>{accepted}</accept></alias>\n"
    )


def _dejavu_rule(dejavu: str, liberation: str) -> str:
    """Insert ``liberation`` right before ``dejavu`` in the family list (not at its front).

    fontconfig edits at the value matched by the first test on the same element, so the
    ``qual="any"`` test must come first; the ``qual="first"`` one keeps an explicit DejaVu request.
    """
    return (
        '  <match target="pattern">\n'
        f'    <test name="family" qual="any"><string>{escape(dejavu)}</string></test>\n'
        f'    <test name="family" qual="first" compare="not_eq"><string>{escape(dejavu)}</string>'
        "</test>\n"
        f'    <edit name="family" mode="prepend" binding="same">'
        f"<string>{escape(liberation)}</string></edit>\n"
        "  </match>\n"
    )


def _dir_rule(fonts_dir: Path | None) -> str:
    if fonts_dir is None:
        return ""
    return f"  <dir>{escape(str(fonts_dir))}</dir>\n"


def build_fontconfig(installed: Collection[str], fonts_dir: Path | None = None) -> str:
    """fontconfig XML: the system configuration, the project fonts directory (``<dir>``, when
    given) and alias rules for the installed families."""
    present = set(installed)
    parts = [
        '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n',
        f'<fontconfig>\n  <include ignore_missing="yes">{SYSTEM_FONTS_CONF}</include>\n',
        _dir_rule(fonts_dir),
    ]
    for family, chain in FALLBACK_CHAINS.items():
        usable = tuple(name for name in chain if name in present)
        if usable:
            parts.append(_alias_rule(family, usable))
    parts.extend(_dejavu_rule(d, lib) for d, lib in DEJAVU_TO_LIBERATION if lib in present)
    parts.append("</fontconfig>\n")
    return "".join(parts)


def dir_only_fontconfig(fonts_dir: Path, cache_dir: Path) -> str:
    """fontconfig XML that sees only ``fonts_dir`` (no system fonts), to list what it holds."""
    return (
        '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n'
        f"<fontconfig>\n{_dir_rule(fonts_dir)}"
        f"  <cachedir>{escape(str(cache_dir))}</cachedir>\n</fontconfig>\n"
    )


def write_fontconfig(
    work: Path, installed: Collection[str] | None = None, fonts_dir: Path | None = None
) -> Path:
    """Write ``fonts.conf`` into ``work`` (private, mode 0600) and return its path.

    Without ``fonts_dir`` the project fonts directory of the environment is used when it exists
    (``$PWNDOC_AI_FONTS_DIR`` or ``$PWNDOC_AI_DATA_DIR/fonts``); its families count as installed.
    """
    directory = fonts_dir if fonts_dir is not None else default_fonts_dir()
    known = set(installed_families() if installed is None else installed)
    if installed is None and directory is not None:
        known |= _project_families(work, directory)
    path = work / FONTS_CONF_NAME
    path.write_text(build_fontconfig(known, directory), encoding="utf-8")
    path.chmod(0o600)
    return path


def _project_families(work: Path, directory: Path) -> frozenset[str]:
    conf = work / "fonts-dir.conf"
    conf.write_text(dir_only_fontconfig(directory, work / "fc-cache"), encoding="utf-8")
    conf.chmod(0o600)
    return _list_families(conf)


def valid_family(family: object) -> bool:
    """A family name that is safe to hand to ``fc-match``: it cannot be read as an option
    (leading '-') or as fontconfig pattern syntax (':', ',', '=', quotes, backslash)."""
    if not isinstance(family, str) or not family.strip() or len(family) > MAX_FAMILY_LENGTH:
        return False
    if family.startswith("-") or any(ch in FORBIDDEN_IN_FAMILY for ch in family):
        return False
    return all(ch.isprintable() for ch in family)


def _fc_match(conf: Path, family: str, timeout: float) -> str | None:
    if not valid_family(family):
        return None
    binary = shutil.which("fc-match")
    if not binary:
        return None
    environment = {"PATH": os.environ.get("PATH", ""), "FONTCONFIG_FILE": str(conf)}
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell, name checked, after "--"
            [binary, "-f", "%{family[0]}", "--", family.replace("-", "\\-")],
            capture_output=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("font check: fc-match failed (%s)", type(exc).__name__)
        return None
    answer = done.stdout.decode("utf-8", "replace").strip()
    return answer[:64] if done.returncode == 0 and answer else None


def fc_matcher(conf: Path, deadline: float | None = None) -> Callable[[str], str | None]:
    """A cached ``family -> substitute`` lookup with ``fc-match`` under the given configuration.

    With a ``deadline`` (``time.monotonic()`` value) no lookup waits beyond it, and none starts
    after it: the answer is then None (unknown), never an error.
    """

    @functools.lru_cache(maxsize=MAX_FAMILIES)
    def match(family: str) -> str | None:
        timeout = FC_TIMEOUT
        if deadline is not None:
            timeout = min(FC_TIMEOUT, deadline - time.monotonic())
            if timeout <= 0:
                return None
        return _fc_match(conf, family, timeout)

    return match


# -- fonts a document asks for ------------------------------
def _part_texts(docx: bytes, *, theme_only: bool = False) -> Iterator[str]:
    """The text of the word/*.xml parts, one at a time (only one is in memory), until the
    total byte budget is spent."""
    budget = MAX_TOTAL_BYTES
    try:
        with zipfile.ZipFile(io.BytesIO(docx)) as archive:
            for info in archive.infolist()[:MAX_MEMBERS]:
                if not PART_NAME.match(info.filename) or info.filename in SKIPPED_PARTS:
                    continue
                if (theme_only and not info.filename.startswith("word/theme/")) or (
                    info.file_size > MAX_PART_BYTES
                ):
                    continue
                budget -= info.file_size
                if budget < 0:
                    log.info("font check: the parts are too large, the rest is not read")
                    return
                with archive.open(info) as handle:
                    yield handle.read(MAX_PART_BYTES).decode("utf-8", "replace")
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError) as exc:
        log.info("font check: document unreadable (%s)", type(exc).__name__)


def _family_names(texts: Iterable[str], pattern: re.Pattern[str]) -> Iterator[str]:
    for text in texts:
        for match in pattern.finditer(text):
            yield match.group(1)


def document_fonts(docx: bytes) -> tuple[str, ...]:
    """Font families the document names (runs, styles, headers, and the theme fonts if used)."""
    seen: dict[str, str] = {}
    themed = False

    def collect(names: Iterable[str]) -> None:
        for name in names:
            cleaned = name.strip()
            key = cleaned.casefold()
            if cleaned and key not in IGNORED_FONTS and not cleaned.startswith(("+", "@")):
                seen.setdefault(key, cleaned)
            if len(seen) >= MAX_FAMILIES:
                return

    for text in _part_texts(docx):
        themed = themed or bool(RFONT_THEME.search(text))
        collect(match.group(1) for match in RFONT_NAME.finditer(text))
        if len(seen) >= MAX_FAMILIES:
            break
    if themed and len(seen) < MAX_FAMILIES:
        collect(_family_names(_part_texts(docx, theme_only=True), THEME_LATIN))
    return tuple(seen.values())


# -- warnings ------------------------------
def _advice(font: str, resolved: str) -> str:
    key = font.casefold()
    if key.startswith(("calibri", "aptos")):
        return (
            f"install {CALIBRI_PACKAGE} (apt) so {font} keeps its line breaks; {resolved} is used"
        )
    if key.startswith("cambria"):
        return (
            f"install {CAMBRIA_PACKAGE} (apt) so {font} keeps its line breaks; {resolved} is used"
        )
    return f"{font} is not installed; {resolved} is used and line breaks may differ"


def _resolve(
    font: str, present: set[str], matcher: Callable[[str], str | None] | None
) -> tuple[str, bool]:
    """(substitute, whether it keeps the metrics) for a font that is not installed."""
    for candidate in FALLBACK_CHAINS.get(_known_name(font), ()):
        if candidate in present:
            return candidate, candidate in METRIC_COMPATIBLE.get(font.casefold(), frozenset())
    found = matcher(font) if matcher else None
    return (found or DEFAULT_RESOLVED), False


def _known_name(font: str) -> str:
    lowered = font.casefold()
    return next((name for name in FALLBACK_CHAINS if name.casefold() == lowered), "")


def font_warnings(
    docx: bytes,
    installed: Collection[str] | None = None,
    *,
    matcher: Callable[[str], str | None] | None = None,
) -> tuple[FontIssue, ...]:
    """One ``FontIssue`` per requested font that will be rendered with a non-compatible font."""
    present = set(installed_families() if installed is None else installed)
    folded = {name.casefold() for name in present}
    issues = []
    for font in document_fonts(docx):
        if font.casefold() in folded:
            continue
        resolved, compatible = _resolve(font, present, matcher)
        if not compatible:
            issues.append(FontIssue(font, resolved, False, _advice(font, resolved)))
    return tuple(issues)
