"""Tagged replacements of the deliverable-1 builders whose content loops over findings.

``patched_builders()`` swaps them into pd_findings / pd_front for one build, so the template is
produced by the same code (styles, widths, paddings, fills) as the Word report.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pd_findings as pf
import pd_front
from ox import Cell, Font, Para, Row, para, run, table
from pd_common import nested
from pd_flow import Flow
from pd_styles import BORDER, INK2, PANEL, SEV_A, SEV_B
from pwt_tags import (
    FIELDS,
    FINDING_HEADING,
    FINDING_ID,
    LOOP,
    PRIORITY_BANDS,
    REGISTER_LOC,
    SEVERITIES,
    UNKNOWN_SEV,
    count,
    in_band,
    rich,
    sev_is,
)


def _p(text: str, fmt: Para, font: Font | None = None, style: str | None = None) -> object:
    return para([run(text, font, style)], fmt)


def section(tag: str, body: list, fmt: Para) -> list:
    """Paragraph-level section: ``{#tag}`` / body / ``{/tag}`` (marker paragraphs vanish)."""
    return [_p(f"{{#{tag}}}", fmt), *body, _p(f"{{/{tag}}}", fmt)]


def inverted(tag: str, body: list, fmt: Para) -> list:
    return [_p(f"{{^{tag}}}", fmt), *body, _p(f"{{/{tag}}}", fmt)]


# ---------------------------------------------------------------- register


def _sev_row_cond(sev: str | None) -> str:
    return sev_is(sev, "'") if sev else UNKNOWN_SEV


def _register_row(sev: str | None) -> Row:
    """One register row; shown when the finding's severity is ``sev`` (None = any other)."""
    body = Para(style="RegisterText", line=11.5)
    loc = [run(line + ("\n" if i < 2 else "")) for i, line in enumerate(REGISTER_LOC)]
    sev_text = sev or FIELDS["severity_any"]
    tint = SEV_B["Info" if sev == "Informational" else sev][1] if sev else None
    cond = _sev_row_cond(sev)
    cells = [
        Cell([para([run(f"{{#{cond}}}"), run(FINDING_ID)], body)], valign="center"),
        Cell([para(loc, Para(style="RegisterText", line=13.5))], pad=(0.9, 4.8, 1.0, 4.8)),
        Cell([_p("{title}", body)], valign="center"),
        Cell([_p(FIELDS["cwe"], body)], valign="center"),
        Cell([para([run(sev_text), run(f"{{/{cond}}}")], body)], shading=tint, valign="center"),
    ]
    return Row(cells, height=pf.REGISTER_ROW - pf.LO_ROW_BORDER, rule="atLeast")


def _empty_register_row() -> Row:
    body = Para(style="RegisterText", line=11.5)
    cells = [Cell([_p(f"{{^{LOOP}}}-", body)], valign="center")]
    cells += [Cell([_p("No vulnerabilities were identified.", body)], span=3, valign="center")]
    cells += [Cell([_p(f"-{{/{LOOP}}}", body)], valign="center")]
    return Row(cells, height=pf.REGISTER_ROW - pf.LO_ROW_BORDER, rule="atLeast")


def register_table(_items: list) -> object:
    """Header row + one row group per finding (one severity-tinted row survives per finding)."""
    rows = [_head_row()]
    group = [_register_row(sev) for sev in (*SEVERITIES, None)]
    group[0].cells[0].content[0].insert(1, run(f"{{#{LOOP}}}"))
    group[-1].cells[-1].content[0].append(run(f"{{/{LOOP}}}"))
    rows += group
    rows.append(_empty_register_row())
    return table(
        pf.REGISTER_WIDTHS, rows, indent=pf.TABLE_INDENT, borders=pf.GRID, pad=(0, 4.8, 0, 4.8)
    )


def _head_row() -> Row:
    labels = (
        "S. No.",
        "Vulnerable location, parameter, path",
        "Name of the vulnerability",
        "Reference \n(CWE-ID)",
        "Severity",
    )
    return Row(
        [pf._head_cell(text) for text in labels],
        height=pf.REGISTER_HEAD - pf.LO_ROW_BORDER,
        rule="exact",
        header=True,
    )


# ---------------------------------------------------------------- detail tables


def _rich_cell(var: str, style: str, images: bool) -> Cell:
    """HTML chunks of ``var`` (text + its images with captions), 'N/A' when empty."""
    marker = Para(style=style)
    body = [para([run(rich(style))], marker)]
    if images:
        pic = [para([run("{%image}")], Para(align="center", before=3.3))]
        pic.append(_p("{caption}", Para(style="Caption", before=1.4)))
        body += section("images", pic, marker)
    content = section(var, body, marker) + inverted(var, [_p("N/A", marker)], marker)
    return Cell(content)


def _severity_value(sev: str | None) -> Cell:
    if sev is None:
        return pf._value(FIELDS["severity_any"])
    key = "Info" if sev == "Informational" else sev
    style = f"Severity{key}"
    return Cell([para([run(sev, style=style)], Para(style="TableValue"))], shading=SEV_B[key][1])


def detail_rows(_ctx: object, _item: dict) -> list[tuple[str, Cell]]:
    """The 11 labelled rows; 'Severity' is expanded into conditional variants later, and
    'Vulnerable Point / Error Location' is a nested sub-table (Location / URL / Parameter)."""
    return [
        ("S. No.", pf._value(FINDING_ID)),
        ("Name of Vulnerability", pf._value("{title}", Font(bold=True))),
        (pf.GROUP_LABEL, _group_cell()),
        ("CVE / CWE", pf._value(FIELDS["cwe"])),
        ("CVSS Score", pf._value(FIELDS["cvss"])),
        ("Severity", _severity_value(None)),
        ("Description", _rich_cell("description", "TableValue", images=True)),
        ("Proof of Concept", _rich_cell("poc", "TableValue", images=True)),
        ("Steps to Reproduce", _rich_cell("stepstoreproduce", "TableList", images=True)),
        ("Recommendations", _rich_cell("remediation", "TableList", images=False)),
        ("References", _references_cell()),
    ]


def _group_cell() -> Cell:
    """Tagged version of ``pf._group_cell``: a nested sub-table with the three tags."""
    grid = {side: (0.5, BORDER) for side in ("top", "left", "bottom", "right", "insideH", "insideV")}
    sub_rows = [
        Row([pf._label("Vulnerable Location"), pf._value(FIELDS["location"])]),
        Row([pf._label("Vulnerable Path / URL"), pf._value(FIELDS["url"], style="TableURL")]),
        Row([pf._label("Vulnerable Parameter"), pf._value(FIELDS["parameter"])]),
    ]
    inner = table(pf.GROUP_SUB_WIDTHS, sub_rows, borders=grid)
    return Cell(nested(inner))


def _references_cell() -> Cell:
    fmt = Para(style="TableValue")
    items = section("references", [_p("{.}", fmt)], fmt)
    return Cell(items + inverted("references", [_p("N/A", fmt)], fmt))


def severity_rows() -> list[Row]:
    """Conditional 'Severity' rows: exactly one renders per finding."""
    out = []
    for sev in (*SEVERITIES, None):
        cond = _sev_row_cond(sev)
        label = Cell(
            [para([run(f"{{#{cond}}}"), run("Severity")], Para(style="TableLabel"))],
            shading=pf.LABEL_FILL,
        )
        value = _severity_value(sev)
        value.content[-1].append(run(f"{{/{cond}}}"))
        out.append(Row([label, value]))
    return out


RICH_LABELS = {"Description", "Proof of Concept", "Steps to Reproduce", "Recommendations"}


def detail_table(ctx: object, item: dict) -> object:
    """13-row detail table; the Severity row becomes six conditional rows, rich rows may split."""
    rows: list[Row] = []
    for label, value in detail_rows(ctx, item):
        if label == "Severity":
            rows += severity_rows()
            continue
        if label == "Proof of Concept":
            rows += poc_rows(value)
            continue
        rows.append(Row([pf._label(label), value], cant_split=label not in RICH_LABELS))
    return table(pf.DETAIL_WIDTHS, rows, indent=pf.TABLE_INDENT, borders=pf.GRID, pad=pf.CELL_PAD)


def poc_rows(value: Cell) -> list[Row]:
    """PoC as a full-width label row + content row: PwnDoc sizes images up to 600 px (450 pt)
    wide, wider than the 370 pt value column, so the evidence spans the table (495 pt)."""
    label = pf._label("Proof of Concept")
    label.span = 2
    value.span = 2
    return [Row([label]), Row([value], cant_split=False)]


def finding(ctx: object, item: dict, first: bool) -> None:
    """Heading + detail table, wrapped in the paragraph loop over findings."""
    fmt = Para(style="Heading3", before=13.25, after=6.25)
    ctx.add(_p(f"{{#{LOOP}}}", Para(style="LegacyBody", line=1)))
    ctx.add(_p(FINDING_HEADING, fmt))
    ctx.add(detail_table(ctx, item))
    ctx.add(_p(f"{{/{LOOP}}}", Para(style="LegacyBody", line=1)))
    # paragraphLoop deletes the end-tag paragraph: the section break needs its own carrier
    ctx.add(para(fmt=Para(line=1, after=0)))


# ---------------------------------------------------------------- page 3


def legend_cell(sev: str, _count: int) -> list:
    flow = Flow(pd_front.BAR_TOP + 15)
    dot = pd_front.inline_shape(
        9, 9, pd_front.ShapeStyle(prst="ellipse", fill=SEV_A[sev]), f"{sev} dot"
    )
    name = para(
        [dot, run("\t" + sev, Font(pd_front.LATO, 9, INK2))],
        flow.fmt(Para(line=12, tabs=((14, "left"),)), 475.65),
    )
    number = para(
        [run(count(sev), Font(pd_front.LATO_HEAVY, 13.5, pd_front.INK))],
        flow.fmt(Para(line=16, left=14), 494.0),
    )
    return [name, number]


HIGHLIGHT_MIN = 60.0  # deliverable 1: exact 85; text-driven here, the callout follows it


def highlight_card(_meta: dict) -> object:
    """'Assessment highlights' section text, else the critical/high finding titles."""
    fmt = Para(style="BodyText", line=14, after=6)
    auto = [_p("{displaySeverity} finding: {title}", fmt)]
    expr = in_band(PRIORITY_BANDS)
    fallback = section(expr, auto, fmt)
    fallback += inverted(expr, [_p("No critical or high findings were identified.", fmt)], fmt)
    content = section("highlights.text", [_p(rich("BodyText"), fmt)], fmt)
    content += inverted("highlights.text", fallback, fmt)
    cell = Cell(
        content, shading=PANEL, borders={"left": (4, SEV_A["Critical"])}, pad=(11, 12, 4, 12)
    )
    return table([526], [Row([cell], height=HIGHLIGHT_MIN, rule="atLeast")])


@contextlib.contextmanager
def patched_builders() -> Iterator[None]:
    """Temporarily route deliverable-1's per-finding builders to the tagged versions."""
    swaps = [
        (pf, "_register_table", register_table),
        (pf, "finding", finding),
        (pd_front, "_legend_cell", legend_cell),
        (pd_front, "_highlight_card", highlight_card),
    ]
    saved = [(mod, name, getattr(mod, name)) for mod, name, _new in swaps]
    try:
        for mod, name, new in swaps:
            setattr(mod, name, new)
        yield
    finally:
        for mod, name, old in saved:
            setattr(mod, name, old)
