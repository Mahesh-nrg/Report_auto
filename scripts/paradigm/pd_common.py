"""Shared context and small composite builders (H1/H2, card cells, TC entries)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from docx.document import Document
from lxml import etree
from ox import Cell, Font, Para, field_runs, para, run
from pd_flow import Flow
from pd_styles import INK, Numbering

ASSETS = Path(__file__).resolve().parent / "assets"


@dataclass
class Ctx:
    doc: Document
    numbering: Numbering
    content: dict
    meta: dict
    body: list = field(default_factory=list)

    def image(self, name: str) -> str:
        rid, _img = self.doc.part.get_or_add_image(str(ASSETS / name))
        return rid

    def add(self, *nodes: etree._Element) -> etree._Element:
        self.body.extend(nodes)
        return nodes[-1]


def tc_entry(text: str, level: int = 1, ident: str = "p") -> list[etree._Element]:
    """A TC field (table-of-contents entry) feeding the 'TOC \\f p' index on page 2."""
    escaped = text.replace('"', "'")
    return field_runs(f'TC "{escaped}" \\f {ident} \\l {level}', [])


def h1(
    ctx: Ctx,
    flow: Flow,
    text: str,
    baseline: float,
    *,
    size: float = 29,
    first_in_section: bool = True,
    tc: list[tuple[str, int]] = (),
    line: float = 36,
    lines: int = 1,
) -> etree._Element:
    fmt = Para(style="Heading1", line=line)
    if first_in_section:
        fmt = fmt.with_(page_break_before=False)
    fmt = flow.fmt(fmt, baseline, lines=lines)
    runs = [run(text, Font(size=size) if size != 29 else None)]
    for entry, level in tc:
        runs.extend(tc_entry(entry, level))
    return ctx.add(para(runs, fmt))


def kicker(
    ctx: Ctx,
    flow: Flow,
    text: str,
    baseline: float,
    left: float = 1,
    font: Font | None = None,
    line: float = 11,
) -> etree._Element:
    fmt = flow.fmt(Para(style="Kicker", left=left, line=line), baseline)
    return ctx.add(para([run(text, font)], fmt))


def h2(
    ctx: Ctx, flow: Flow, number: str, title: str, baseline: float, tc: list[tuple[str, int]] = ()
) -> etree._Element:
    fmt = flow.fmt(Para(style="Heading2"), baseline)
    runs = [run(number, style="H2Number"), run("\t" + title)]
    for entry, level in tc:
        runs.extend(tc_entry(entry, level))
    return ctx.add(para(runs, fmt))


def text_para(
    ctx: Ctx,
    flow: Flow,
    style: str,
    text: str,
    baseline: float,
    lines: int,
    line: float,
    font: Font | None = None,
    **kw: object,
) -> etree._Element:
    fmt = flow.fmt(Para(style=style, line=line, **kw), baseline, lines=lines)  # type: ignore[arg-type]
    return ctx.add(para([run(text, font)], fmt))


def cell_paras(
    top: float, items: list[tuple[str, str, float, Font | None]], fmt: Para | None = None
) -> list[etree._Element]:
    """Paragraphs inside a cell whose content box starts at ``top``.

    ``items`` = (style, text, baseline, font); each paragraph is one line high.
    """
    flow = Flow(top)
    out = []
    base = fmt or Para()
    for style, text, baseline, font in items:
        line = _line_of(style, font)
        para_fmt = base.with_(style=style, line=line)
        para_fmt = para_fmt.with_(before=flow.place(baseline, line))
        out.append(para([run(text, font)], para_fmt))
    return out


LINE_FACTOR = 1.25


def _line_of(style: str, font: Font | None) -> float:
    size = font.size if font and font.size else STYLE_SIZES.get(style, 10)
    return round(size * LINE_FACTOR, 2)


STYLE_SIZES = {
    "CardLabel": 8,
    "CardValue": 11,
    "CoverCardLabel": 8.5,
    "CoverCardValue": 16,
    "Chip": 7.5,
    "TableLabel": 9,
    "TableValue": 9,
}


def shaded_cell(
    content: list, fill: str | None, pad: tuple[float, float, float, float], **kw: object
) -> Cell:
    return Cell(content, shading=fill, pad=pad, **kw)  # type: ignore[arg-type]


INK_FONT = Font(color=INK)
CARRIER = Para(line=1, after=0)


def nested(inner: etree._Element) -> list[etree._Element]:
    """A nested table plus the 1 pt paragraph Word requires after it inside a cell."""
    return [inner, para(fmt=CARRIER)]


def union_grid(*edge_sets: list[float]) -> list[float]:
    """Merged column edges of several rows (pt from the table's left edge)."""
    return sorted({round(edge, 2) for edges in edge_sets for edge in edges})


def spans(grid: list[float], widths: list[float]) -> list[int]:
    """Number of grid columns each cell of ``widths`` covers on ``grid``."""
    out, left = [], 0.0
    for width in widths:
        right = round(left + width, 2)
        out.append(sum(1 for edge in grid if left < edge <= right + 0.01))
        left = right
    return out
