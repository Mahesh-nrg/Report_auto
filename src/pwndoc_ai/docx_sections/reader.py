"""Turn a DOCX into the block list of section detection (contract: EXPORT_DESIGN.md (h)).

One block per top-level body ``w:p``/``w:tbl`` (content controls unwrapped), ids ``b%05d`` from
the SAME enumeration as the typography engine (``docx_typography.blocks.body_blocks``). Header,
footer and footnote parts are not blocks: the typography engine types them itself. The text kept
is an excerpt for display and for the model; the document is never modified.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from lxml import etree

from ..docx_engine import limits as docx_limits
from ..docx_engine.errors import DocxError
from ..docx_engine.package import Package, open_package
from ..docx_engine.xmlutil import attr, local, parse_xml, w
from ..docx_typography.blocks import body_blocks
from ..export_json import CONTROL_LINE
from ..font_catalog_model import valid_family
from .errors import SectionsError
from .model import MAX_BLOCK_LENGTH, MAX_BLOCKS, MAX_EXCERPT_CHARS, MAX_STYLE_CHARS, Block
from .style_info import StyleBook, StyleInfo, ascii_font, half_points, on_off, outline_level
from .style_info import read_styles as parse_styles

STYLES_PART = "word/styles.xml"
MONO_FONT = re.compile(
    r"mono|courier|consolas|menlo|monaco|inconsolata|lucida console|fira code|\bhack\b|source code",
    re.IGNORECASE,
)
CELL_SEPARATOR = " | "
MAX_CELLS = 40
TEXT_TAGS = {"t": None, "tab": " ", "br": " ", "cr": " ", "noBreakHyphen": "-"}


@dataclass(frozen=True)
class ReadResult:
    """The blocks, plus the ids of paragraphs that hold only a drawing (figures)."""

    blocks: tuple[Block, ...]
    figures: frozenset[str]


@dataclass(frozen=True)
class _Run:
    chars: int
    size: float | None
    bold: bool
    font: str
    mono: bool


def _excerpt(text: str) -> str:
    return " ".join(CONTROL_LINE.sub(" ", text).split())[:MAX_EXCERPT_CHARS]


def _run_text(run: etree._Element) -> str:
    parts: list[str] = []
    for child in run:
        name = local(child)
        if name in TEXT_TAGS:
            parts.append(child.text or "" if TEXT_TAGS[name] is None else TEXT_TAGS[name])
    return "".join(parts)


def _run_facts(run: etree._Element, text: str, style: StyleInfo) -> _Run:
    rpr = run.find(w("rPr"))
    bold = on_off(rpr.find(w("b"))) if rpr is not None else None
    explicit = ascii_font(rpr)
    shown = explicit or style.font
    return _Run(
        chars=len(text),
        size=half_points(rpr) or style.size,
        bold=bool(style.bold) if bold is None else bold,
        font=explicit,
        mono=bool(shown and MONO_FONT.search(shown)),
    )


def _dominant(runs: list[_Run], key: str) -> object:
    weights: Counter = Counter()
    for item in runs:
        weights[getattr(item, key)] += item.chars
    return weights.most_common(1)[0][0] if weights else None


def _majority(runs: list[_Run], key: str) -> bool:
    total = sum(item.chars for item in runs)
    return total > 0 and sum(item.chars for item in runs if getattr(item, key)) * 2 > total


def _clean_font(name: object) -> str:
    text = str(name or "").strip()
    return text if text and valid_family(text) else ""


def _paragraph_style(para: etree._Element) -> tuple[etree._Element | None, str]:
    ppr = para.find(w("pPr"))
    node = ppr.find(w("pStyle")) if ppr is not None else None
    return ppr, (attr(node, "val") or "") if node is not None else ""


def _text_runs(para: etree._Element) -> list[tuple[etree._Element, bool]]:
    """The runs of ``para`` (text-box paragraphs included, ``mc:Fallback`` copies skipped), each
    flagged when it starts another paragraph: nested paragraphs are joined with a space, so two
    lines of a text box never fuse into one word (and a masked name never escapes the masker)."""
    found: list[tuple[etree._Element, bool]] = []
    previous = None
    for run in para.iter(w("r")):
        owner, skipped = None, False
        for ancestor in run.iterancestors():
            name = local(ancestor)
            if name == "Fallback":
                skipped = True
                break
            if owner is None and name == "p":
                owner = ancestor
            if ancestor is para:
                break
        if skipped:
            continue
        found.append((run, previous is not None and owner is not previous))
        previous = owner
    return found


def _paragraph(block_id: str, para: etree._Element, book: StyleBook) -> tuple[Block, bool]:
    ppr, style_id = _paragraph_style(para)
    style = book.get(style_id)
    texts, runs = [], []
    for run, separate in _text_runs(para):
        text = _run_text(run)
        if text:
            texts.append(f" {text}" if separate and texts else text)
            runs.append(_run_facts(run, text, style))
    full = "".join(texts)
    numbered = ppr is not None and ppr.find(w("numPr")) is not None
    level = outline_level(ppr) or style.level
    size = _dominant(runs, "size") if runs else style.size
    block = Block(
        id=block_id,
        kind="paragraph",
        text=_excerpt(full),
        style=_excerpt(style.name or style_id)[:MAX_STYLE_CHARS],
        level=level,
        length=min(len(full), MAX_BLOCK_LENGTH),
        bold=_majority(runs, "bold"),
        size=float(size) if isinstance(size, int | float) else None,
        list=numbered or style.numbered or style.name.lower().startswith("list"),
        mono=_majority(runs, "mono"),
        font=_clean_font(_dominant(runs, "font")),
    )
    drawing = not full.strip() and any(True for _ in para.iter(w("drawing"), w("pict")))
    return block, drawing


def _cell_texts(table: etree._Element) -> list[str]:
    cells = []
    for cell in table.iter(w("tc")):
        text = "".join(_run_text(run) for run in cell.iter(w("r")))
        if text.strip():
            cells.append(text)
    return cells


def _table(block_id: str, table: etree._Element, book: StyleBook) -> Block:
    cells = _cell_texts(table)
    tpr = table.find(w("tblPr"))
    node = tpr.find(w("tblStyle")) if tpr is not None else None
    style_id = (attr(node, "val") or "") if node is not None else ""
    found = book.styles.get(style_id)
    name = found.name if found is not None and found.name else style_id
    return Block(
        id=block_id,
        kind="table",
        text=_excerpt(CELL_SEPARATOR.join(cells[:MAX_CELLS])),
        style=_excerpt(name)[:MAX_STYLE_CHARS],
        length=min(sum(len(cell) for cell in cells), MAX_BLOCK_LENGTH),
    )


def _parts(data: bytes) -> tuple[etree._Element, StyleBook]:
    try:
        package: Package = open_package(data)
        root = parse_xml(package.read(package.main_part, docx_limits.MAX_XML_BYTES))
        styles = None
        if STYLES_PART in package.names:
            styles = parse_xml(package.read(STYLES_PART, docx_limits.MAX_SMALL_XML_BYTES))
    except DocxError:
        raise SectionsError("unreadable") from None
    return root, parse_styles(styles)


def read_document(data: bytes) -> ReadResult:
    """All blocks and the figure ids. Raises :class:`SectionsError` (``unreadable``,
    ``too_complex``, ``no_text``)."""
    root, book = _parts(data)
    pairs = body_blocks(root)
    if len(pairs) > MAX_BLOCKS:
        raise SectionsError("too_complex")
    blocks: list[Block] = []
    figures: set[str] = set()
    for block_id, element in pairs:
        if local(element) == "tbl":
            blocks.append(_table(block_id, element, book))
            continue
        block, drawing = _paragraph(block_id, element, book)
        blocks.append(block)
        if drawing:
            figures.add(block_id)
    if not any(block.text for block in blocks):
        raise SectionsError("no_text")
    return ReadResult(tuple(blocks), frozenset(figures))


def read_blocks(data: bytes) -> tuple[Block, ...]:
    """The blocks of a DOCX (contract signature)."""
    return read_document(data).blocks
