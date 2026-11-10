"""Cover (page 1), 'Inside this report' (page 2) and 'Executive overview' (page 3)."""

from __future__ import annotations

from ox import (
    Box,
    Cell,
    Font,
    Para,
    Row,
    ShapeStyle,
    anchored_picture,
    field_runs,
    inline_shape,
    para,
    run,
    table,
)
from pd_common import Ctx, cell_paras, h1, h2, kicker, nested, spans, text_para, union_grid
from pd_flow import Flow, add_after
from pd_styles import (
    BANNER,
    BLUE,
    CARD2,
    CARD_NAVY,
    INK,
    INK2,
    INK3,
    LATO,
    LATO_BLACK,
    LATO_HEAVY,
    LATO_MED,
    MINT,
    PANEL,
    SEV_A,
    TEAL,
)

SEVERITIES = ("Critical", "High", "Medium", "Low", "Informational")


def cover(ctx: Ctx) -> None:
    meta, flow = ctx.meta, Flow(0)
    background = anchored_picture(
        ctx.image("cover_background_full_bleed_200dpi.png"),
        Box(0, 0, 612, 792),
        "Cover background",
        "Navy cover background with concentric orbit art",
        z=10,
    )
    logo = anchored_picture(
        ctx.image("logo_cover_white.png"),
        Box(46, 46.95, 222, 50),
        "ParadigmIT logo (white)",
        "ParadigmIT Cybersecurity logo",
        z=20,
    )
    kick = flow.fmt(Para(style="CoverKicker", left=3.1), 167.75)
    ctx.add(para([background, logo, run("SECURITY ASSESSMENT")], kick))
    title = flow.fmt(Para(style="Title", left=0.1), 247.8, lines=4)
    ctx.add(para([run("Vulnerability\nAssessment &\nPenetration\nTesting")], title))
    sub = flow.fmt(Para(style="Subtitle", left=1.1), 460.8)
    ctx.add(para([run(meta["application"])], sub))
    meta_p = ctx.add(
        para([run(meta["cover_meta"])], flow.fmt(Para(style="CoverMeta", left=2.1), 488.7))
    )
    add_after(meta_p, flow.block(583, 113))
    ctx.add(_cover_card(meta))
    foot = flow.fmt(Para(style="CoverFooter", left=2.1), 744.6)
    ctx.add(para([run("PRIVATE & CONFIDENTIAL\tPARADIGMIT CYBERSECURITY")], foot))


def _cover_card(meta: dict) -> object:
    top = 583.0
    left = cell_paras(
        top,
        [
            ("CoverCardLabel", "ASSESSMENT WINDOW", 609.6, None),
            ("CoverCardValue", meta["window_upper"], 639.95, Font(size=13)),
        ],
    )
    right = cell_paras(
        top,
        [
            ("CoverCardLabel", "PREPARED FOR", 609.6, None),
            ("CoverCardValue", meta["client"], 640.15, None),
        ],
    )
    accent = {"left": (4, MINT)}
    cells = [
        Cell(left, shading=CARD_NAVY, borders=accent, pad=(0, 16, 0, 0)),
        Cell(right, shading=CARD_NAVY, pad=(0, 20, 0, 0)),
    ]
    return table([258, 264], [Row(cells, height=113)], indent=-1)


# ---------------------------------------------------------------- page 2


INDEX = (
    ("01", "Executive summary", "03", ("Assumptions; summary of findings", "03")),
    (
        "02",
        "Severity scoring and scope",
        "04",
        ("Constraints, targets, environment, duration", "04"),
    ),
    ("03", "Approach and methodology", "04", None),
    ("04", "Testing tools", "05", None),
    ("05", "Vulnerability location register", "07", None),
    ("06", "Detailed technical findings & evidence", "08", None),
    ("07", "Disclaimer", "24", None),
    ("08", "Credentials & assurance", "25", None),
)
INDEX_TITLE_BASE = 311.9
INDEX_PITCH, INDEX_SUB_GAP = 35.0, 34.8
# Direct formatting repeats the character styles: LibreOffice rebuilds TOC field results and
# drops character styles from the cached runs.
INDEX_NUMBER, INDEX_PAGE = Font(LATO_BLACK, 9.5, TEAL), Font(LATO, 11, BLUE)


def inside_report(ctx: Ctx) -> None:
    meta, flow = ctx.meta, Flow(72)
    h1(ctx, flow, "Inside this report", 107.25, size=30, first_in_section=True)
    text_para(
        ctx,
        flow,
        "Lead",
        "An executive view first. The evidence, supporting observations "
        "and technical recommendations follow in their existing assessment format.",
        132.95,
        2,
        14,
        right=39,
    )
    add_after(ctx.body[-1], flow.block(178, 59))
    ctx.add(_info_cards(meta))
    text_para(ctx, flow, "IndexLabel", "REPORT INDEX", 275.65, 1, 11)
    _index(ctx, flow)
    add_after(ctx.body[-1], flow.block(690, 38))
    ctx.add(_mint_banner(meta))


def _info_cards(meta: dict) -> object:
    cards = (
        ("CLIENT", meta["client"], 11),
        ("APPLICATION", meta["application"], 10),
        ("ASSESSMENT", meta["assessment_short"], 11),
    )
    cells: list[Cell] = []
    for index, (label, value, size) in enumerate(cards):
        if index:
            cells.append(Cell([para()]))
        content = cell_paras(
            178, [("CardLabel", label, 197.6, None), ("CardValue", value, 218.8, Font(size=size))]
        )
        cells.append(Cell(content, shading=CARD2, pad=(0, 11, 0, 4)))
    return table([166, 11, 166, 11, 166], [Row(cells, height=59)])


def _index_entries(flow: Flow) -> list:
    nodes, base = [], INDEX_TITLE_BASE
    for number, title, page, sub in INDEX:
        fmt = flow.fmt(Para(style="TOC1", line=15), base)
        nodes.append(
            [
                run(number, INDEX_NUMBER, style="IndexNumber"),
                run("\t" + title),
                run("\t"),
                run(page, INDEX_PAGE, style="IndexPage"),
            ]
        )
        nodes[-1] = (nodes[-1], fmt)
        if sub:
            sub_fmt = flow.fmt(Para(style="TOC2", line=12), base + INDEX_SUB_GAP)
            nodes.append(
                ([run(sub[0]), run("\t"), run(sub[1], Font(LATO_MED, 9.5, INK3))], sub_fmt)
            )
            base += INDEX_SUB_GAP + (64 - INDEX_SUB_GAP)
        else:
            base += INDEX_PITCH
    return nodes


def _index(ctx: Ctx, flow: Flow) -> None:
    """The report index: a real TOC field over TC entries, cached result = the PDF's index."""
    entries = _index_entries(flow)
    begin = field_runs('TOC \\f p \\l "1-2" \\n 2-2', [])
    head, tail = begin[:3], begin[3:]
    for index, (runs, fmt) in enumerate(entries):
        prefix = head if index == 0 else []
        suffix = tail if index == len(entries) - 1 else []
        ctx.add(para([*prefix, *runs, *suffix], fmt))


def _mint_banner(meta: dict) -> object:
    text = f"ALL {meta['total']} TECHNICAL FINDINGS AND ORIGINAL EVIDENCE ARE PRESERVED."
    content = cell_paras(690, [("CardLabel", text, 713.0, Font(color="167968"))])
    return table(
        [526], [Row([Cell(content, shading="EAF7F4", pad=(0, 12, 0, 12))], height=38)], indent=0
    )


# ---------------------------------------------------------------- page 3


def executive(ctx: Ctx) -> None:
    meta, flow = ctx.meta, Flow(72)
    h1(
        ctx,
        flow,
        "Executive overview",
        107.15,
        tc=[("01\tExecutive summary", 1), ("Assumptions; summary of findings", 2)],
    )
    kicker(ctx, flow, meta["kicker_p3"], 131.65)
    text_para(
        ctx,
        flow,
        "BodyText",
        meta["exec_summary"],
        168.4,
        3,
        15,
        Font(size=10.5, color=INK),
        right=29,
    )
    add_after(ctx.body[-1], flow.block(242, 137))
    ctx.add(_stat_banner(meta))
    h2(ctx, flow, "01", "Finding distribution", 416.9)
    add_after(ctx.body[-1], flow.block(BAR_TOP, 15 + LEGEND_ROW))
    ctx.add(_distribution(meta))
    h2(ctx, flow, "02", "Assessment highlights", 535.0)
    add_after(ctx.body[-1], flow.block(557, 85))
    ctx.add(_highlight_card(meta))
    _mint_callout(ctx, flow, meta)


def _stat_banner(meta: dict) -> object:
    """Navy stats banner: total findings | priority count with its breakdown (nested)."""
    label = Font(LATO_HEAVY, 8.5, "94B5C0")
    left = cell_paras(242, [("CardLabel", "IDENTIFIED FINDINGS", 275.6, label)], Para(left=5))
    left += cell_paras(277.6, [("CardValue", meta["total"], 344.7, Font(LATO_BLACK, 63, "FFFFFF"))])
    head = cell_paras(
        242,
        [("CardLabel", "REQUIRING PRIORITY ATTENTION", 274.6, Font(LATO_HEAVY, 8, "94B5C0"))],
        Para(left=17),
    )
    add_after(head[-1], 290 - 276.6)
    top2 = 290.0
    prio = cell_paras(top2, [("CardValue", meta["priority"], 336.3, Font(LATO_BLACK, 44, MINT))])
    sub_font = Font(LATO, 13, "FFFFFF")
    subs = cell_paras(
        top2,
        [
            ("CardValue", line, base, sub_font)
            for line, base in zip(meta["priority_lines"], (320.95, 342.95), strict=True)
        ],
    )
    inner = table(
        [78, 233],
        [
            Row(
                [
                    Cell(prio, shading=BANNER, pad=(0, 14, 0, 0)),
                    Cell(subs, shading=BANNER, pad=(0, 17, 0, 0)),
                ],
                height=89,
            )
        ],
    )
    cells = [
        Cell(left, shading=BANNER, pad=(0, 17, 0, 0)),
        Cell([*head, *nested(inner)], shading=BANNER),
    ]
    return table([215, 311], [Row(cells, height=137)])


LEGEND_WIDTHS = [98.0, 94.0, 126.0, 114.0, 94.0]  # dots at x43, 141, 235, 361, 475
BAR_TOP, LEGEND_ROW = 438.0, 60.0


def _distribution(meta: dict) -> object:
    """Proportional severity bar (row 1) and its legend (row 2) on one merged grid."""
    counts = [int(meta["counts"][sev]) for sev in SEVERITIES]
    total = sum(counts) or 1
    bar = [round(526 * c / total, 2) for c in counts]
    bar[-1] = round(526 - sum(bar[:-1]), 2)
    edges = [list(_edges(bar)), list(_edges(LEGEND_WIDTHS))]
    grid = union_grid(*edges)
    widths = [round(b - a, 2) for a, b in zip([0.0, *grid[:-1]], grid, strict=True)]
    bar_cells = [
        Cell([para(fmt=Para(line=1))], shading=SEV_A[sev], span=n)
        for sev, n in zip(SEVERITIES, spans(grid, bar), strict=True)
        if n
    ]
    legend = [
        Cell(_legend_cell(sev, count), span=n)
        for sev, count, n in zip(SEVERITIES, counts, spans(grid, LEGEND_WIDTHS), strict=True)
    ]
    return table(widths, [Row(bar_cells, height=15), Row(legend, height=LEGEND_ROW)])


def _edges(widths: list[float]) -> list[float]:
    out, left = [], 0.0
    for width in widths:
        left = round(left + width, 2)
        out.append(left)
    return out


def _legend_cell(sev: str, count: int) -> list:
    flow = Flow(BAR_TOP + 15)
    dot = inline_shape(9, 9, ShapeStyle(prst="ellipse", fill=SEV_A[sev]), f"{sev} dot")
    name = para(
        [dot, run("\t" + sev, Font(LATO, 9, INK2))],
        flow.fmt(Para(line=12, tabs=((14, "left"),)), 475.65),
    )
    number = para(
        [run(f"{count:02d}", Font(LATO_HEAVY, 13.5, INK))], flow.fmt(Para(line=16, left=14), 494.0)
    )
    return [name, number]


def _highlight_card(meta: dict) -> object:
    top = 557.0
    flow = Flow(top)
    first = flow.fmt(Para(style="BodyText", line=14), 578.8, lines=2)
    second = flow.fmt(Para(style="BodyText", line=14), 627.0)
    content = [
        para([run(meta["highlight_critical"])], first),
        para([run(meta["highlight_high"], Font(LATO, 9.5, INK2))], second),
    ]
    cell = Cell(
        content, shading=PANEL, borders={"left": (4, SEV_A["Critical"])}, pad=(0, 12, 0, 12)
    )
    return table([526], [Row([cell], height=85)])


CALLOUT_PAD = (8.2, 25.8)


def _mint_callout(ctx: Ctx, flow: Flow, meta: dict) -> None:
    """Paragraph box (shading + same-colour borders as padding) under the highlight table."""
    pad_top, pad_bottom = CALLOUT_PAD
    base = Para(style="BodyText", line=14, shading="E9F6F4", left=12, right=12)
    fmt = flow.fmt(base, 685.9, lines=2)
    fmt = fmt.with_(before=max(0.0, (fmt.before or 0) - pad_top - 0.5))
    flow.y += pad_bottom + 0.5
    node = para([run(meta["point_in_time"], Font(color="24564F"))], fmt)
    _box_borders(node, "E9F6F4", top=pad_top, bottom=pad_bottom, side=12)
    ctx.add(node)


def _box_borders(node: object, color: str, top: float, bottom: float, side: float) -> None:
    from ox import el

    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    ppr = node.find(f"{w}pPr")  # type: ignore[attr-defined]
    bdr = el("w:pBdr")
    for edge, space in (("top", top), ("left", side), ("bottom", bottom), ("right", side)):
        bdr.append(
            el(
                f"w:{edge}",
                {"w:val": "single", "w:sz": "4", "w:space": str(round(space)), "w:color": color},
            )
        )
    shading = ppr.find(f"{w}shd")
    shading.addprevious(bdr)
