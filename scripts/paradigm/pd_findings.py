"""Vulnerability register (pages 7-8) and the 19 detailed finding tables (pages 8-23)."""

from __future__ import annotations

from ox import Cell, Font, Para, Row, inline_picture, para, run, table
from pd_common import Ctx, nested, tc_entry
from pd_flow import Flow, add_after
from pd_styles import ALT_ROW, BORDER, HEAD_BLUE, LABEL_FILL, SEV_B

TABLE_INDENT = 5.6  # tables p7-23 sit at x48.6 (margin 43)
GRID = {side: (0.5, BORDER) for side in ("top", "left", "bottom", "right", "insideH", "insideV")}
REGISTER_WIDTHS = (31, 208, 141, 54, 70)
DETAIL_WIDTHS = (125, 379)
CELL_PAD = (0.9, 4.6, 2.15, 4.6)  # single-line row = 0.9 + 12.1 + 2.15 + 0.5 border = 15.65
POC_WIDTH = 356.4
# Register row pitch measured in the PDF (3-line / 4-line location cell, header row). Rows are
# exact-height; LibreOffice lays the 0.5 pt top and bottom borders outside the row height, so
# the exact value is the pitch minus 1 pt (Word draws the borders inside: 1 pt shorter rows).
REGISTER_ROW, REGISTER_ROW_TALL, REGISTER_HEAD = 48.45, 60.0, 38.85
LO_ROW_BORDER = 1.0
LOC_WRAP_CHARS = 55
POC_RATIO = {"004", "010", "014", "016", "018", "019"}  # 1400 x 520 px, others 1400 x 560


def _sev_key(severity: str) -> str:
    return "Info" if severity.lower().startswith("info") else severity.title()


# ---------------------------------------------------------------- register


def register(ctx: Ctx) -> None:
    flow = Flow(72)
    head = Para(style="RegisterHeading")
    runs = [run("5.  List of Vulnerable Parameter / Location Discovered")]
    runs.extend(tc_entry("05\tVulnerability location register", 1))
    ctx.add(para(runs, flow.fmt(head, 94.1)))
    intro = flow.fmt(Para(style="RegisterIntro"), 122.75, lines=2)
    ctx.add(para([run(ctx.meta["register_intro"])], intro))
    add_after(ctx.body[-1], flow.block(155.9, 0))
    ctx.add(_register_table(ctx.content["register"]))


def _head_cell(text: str) -> Cell:
    lines = text.split("\n")
    fmt = Para(style="RegisterHead", line=12.25 if len(lines) > 1 else 11)
    return Cell([para([run(text)], fmt)], shading=HEAD_BLUE, valign="center")


def _loc_lines(lines: list[str]) -> int:
    """Rendered lines of the location cell (Carlito 8 wraps past ~55 characters in 198 pt)."""
    return sum(max(1, -(-len(line) // LOC_WRAP_CHARS)) for line in lines)


def _register_row(index: int, item: dict) -> Row:
    fill = ALT_ROW if index % 2 == 1 else None
    _text, tint = SEV_B[_sev_key(item["sev"])]
    body = Para(style="RegisterText", line=11.5)
    loc = para([run("\n".join(item["loc"]))], Para(style="RegisterText", line=13.5))
    cells = [
        Cell([para([run(item["id"])], body)], shading=fill, valign="center"),
        Cell([loc], shading=fill, pad=(0.9, 4.8, 1.0, 4.8)),
        Cell([para([run(item["name"])], body)], shading=fill, valign="center"),
        Cell([para([run(item["cwe"])], body)], shading=fill, valign="center"),
        Cell([para([run(item["sev"])], body)], shading=tint, valign="center"),
    ]
    height = REGISTER_ROW if _loc_lines(item["loc"]) <= 3 else REGISTER_ROW_TALL
    return Row(cells, height=height - LO_ROW_BORDER, rule="exact")


def _register_table(items: list[dict]) -> object:
    labels = (
        "S. No.",
        "Vulnerable location, parameter, path",
        "Name of the vulnerability",
        "Reference \n(CWE-ID)",
        "Severity",
    )
    rows = [
        Row(
            [_head_cell(text) for text in labels],
            height=REGISTER_HEAD - LO_ROW_BORDER,
            rule="exact",
            header=True,
        )
    ]
    rows.extend(_register_row(index, item) for index, item in enumerate(items))
    return table(REGISTER_WIDTHS, rows, indent=TABLE_INDENT, borders=GRID, pad=(0, 4.8, 0, 4.8))


# ---------------------------------------------------------------- findings


def findings_intro(ctx: Ctx) -> None:
    runs = [run("06  /  DETAILED TECHNICAL FINDINGS")]
    runs.extend(tc_entry("06\tDetailed technical findings & evidence", 1))
    ctx.add(para(runs, Para(style="SectionKicker", before=1.4)))
    ctx.add(para([run(ctx.meta["findings_intro"])], Para(style="LegacyBody", before=11.05)))


def finding(ctx: Ctx, item: dict, first: bool) -> None:
    before = 13.25 if first else 7.63
    title = f"Finding {item['id']} — {item['title']}"
    ctx.add(para([run(title)], Para(style="Heading3", before=before, after=6.25)))
    ctx.add(_detail_table(ctx, item))


def _label(text: str) -> Cell:
    return Cell([para([run(text)], Para(style="TableLabel"))], shading=LABEL_FILL)


def _value(
    text: str, font: Font | None = None, style: str = "TableValue", shading: str | None = None
) -> Cell:
    return Cell([para([run(text, font)], Para(style=style))], shading=shading)


# "Vulnerable Point / Error Location" sub-table (3 label/value rows nested in the value cell of
# the parent row), matching the brandarmy-style report layout.
GROUP_LABEL = "Vulnerable Point / Error Location"
GROUP_SUB_WIDTHS = (110, 269)
GROUP_SUB_GRID = {side: (0.5, BORDER) for side in ("top", "left", "bottom", "right", "insideH", "insideV")}


def _group_cell(item: dict) -> Cell:
    sub_rows = [
        Row([_label("Vulnerable Location"), _value(item["location"])]),
        Row([_label("Vulnerable Path / URL"), _value("\n".join(item["paths"]), style="TableURL")]),
        Row([_label("Vulnerable Parameter"), _value(item["parameter"])]),
    ]
    inner = table(GROUP_SUB_WIDTHS, sub_rows, borders=GROUP_SUB_GRID)
    return Cell(nested(inner))


def _list_cell(ctx: Ctx, items: list[str]) -> Cell:
    num = ctx.numbering.new_list()
    fmt = Para(style="TableList", num=(num, 0))
    return Cell([para([run(text)], fmt) for text in items])


def _poc_cell(ctx: Ctx, item: dict) -> Cell:
    src = item.get("src_id", item["id"])  # evidence files keep the source S. No.
    height = round(POC_WIDTH * (520 if src in POC_RATIO else 560) / 1400, 2)
    rid = ctx.image(f"poc_{src}.png")
    picture = inline_picture(
        rid,
        POC_WIDTH,
        height,
        f"PoC {item['id']}",
        f"Proof of concept evidence for finding {item['id']}",
    )
    image = para([picture], Para(align="center", before=3.3, line=1, line_rule="auto"))
    caption = para([run(item["caption"])], Para(style="Caption", before=1.4))
    return Cell([image, caption])


def _detail_rows(ctx: Ctx, item: dict) -> list[tuple[str, Cell]]:
    color, tint = SEV_B[_sev_key(item["severity"])]
    sev_style = f"Severity{_sev_key(item['severity'])}"
    severity = Cell(
        [para([run(item["severity"], style=sev_style)], Para(style="TableValue"))], shading=tint
    )
    return [
        ("S. No.", _value(item["id"])),
        ("Name of Vulnerability", _value(item["title"], Font(bold=True))),
        (GROUP_LABEL, _group_cell(item)),
        ("CVE / CWE", _value(item["cwe"])),
        ("CVSS Score", _value(item["cvss"])),
        ("Severity", severity),
        ("Description", _value(item["description"])),
        ("Proof of Concept", _poc_cell(ctx, item)),
        ("Steps to Reproduce", _list_cell(ctx, item["steps"])),
        ("Recommendations", _list_cell(ctx, item["recommendations"])),
        ("References", _value(item["references"])),
    ]


def _detail_table(ctx: Ctx, item: dict) -> object:
    rows = [Row([_label(label), value]) for label, value in _detail_rows(ctx, item)]
    return table(DETAIL_WIDTHS, rows, indent=TABLE_INDENT, borders=GRID, pad=CELL_PAD)
