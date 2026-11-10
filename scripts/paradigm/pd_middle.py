"""'Assessment framework' (page 4), 'Coverage & tooling' (page 5), 'Remediation priorities' (6)."""

from __future__ import annotations

from ox import Cell, Font, Para, Row, para, run, table
from pd_common import Ctx, cell_paras, h1, h2, kicker, nested, text_para
from pd_flow import Flow, add_after
from pd_styles import (
    BANNER,
    BLUE,
    BULLET_ABSTRACT,
    INK,
    INK2,
    LATO,
    LATO_BLACK,
    LATO_HEAVY,
    LATO_MED,
    MINT,
    PANEL,
    SEV_A,
)

GUIDANCE = (
    ("OWASP Top 10 Application Security Risks - 2025", "OWASP Testing Guide"),
    ("SANS / CWE testing guidance", "Open-Source Security Testing Methodology Manual"),
    ("Industry best practices for web and APIs", None),
)
SEVERITY_ROWS = (
    ("CRITICAL", "Critical", "Immediate threat to key business processes."),
    ("HIGH", "High", "Direct threat to key business processes."),
    ("MEDIUM", "Medium", "Indirect or partial threat to business processes."),
    ("LOW", "Low", "No direct threat; may be combined with other weaknesses."),
    ("INFORMATIONAL", "Informational", "Awareness, best-practice alignment or hardening."),
)
OWASP = (
    ("A01", "Broken Access Control", True),
    ("A02", "Security Misconfiguration", True),
    ("A03", "Software Supply Chain Failures", True),
    ("A04", "Cryptographic Failures", True),
    ("A05", "Injection", True),
    ("A06", "Insecure Design", True),
    ("A07", "Authentication Failures", True),
    ("A08", "Software and Data Integrity Failures", True),
    ("A09", "Security Logging and Alerting Failures", False),
    ("A10", "Mishandling of Exceptional Conditions", True),
)
TOOLS = (
    "Burp Suite Pro (commercial)",
    "Sqlmap",
    "TestSSL",
    "Dirbuster",
    "Nmap",
    "Nikto",
    "Nuclei",
    "Recon FTW",
    "Hydra",
    "Wireshark",
    "XSSER",
    "Sniper",
    "SSH Audit",
    "Data tampering / replay plug-ins",
    "Manual validation & false-positive review",
)


def gap_row(cols: int, height: float) -> Row:
    """A fixed-height white separator row between card rows (no text)."""
    return Row([Cell([para(fmt=Para(line=1))], span=cols)], height=height)


# ---------------------------------------------------------------- page 4


def framework(ctx: Ctx) -> None:
    meta, flow = ctx.meta, Flow(72)
    h1(
        ctx,
        flow,
        "Assessment framework",
        107.15,
        tc=[
            ("02\tSeverity scoring and scope", 1),
            ("Constraints, targets, environment, duration", 2),
        ],
    )
    kicker(ctx, flow, "02  /  SCOPE, METHOD & SEVERITY", 131.65)
    h2(ctx, flow, "01", "Engagement scope", 167.95)
    add_after(ctx.body[-1], flow.block(185, 100))
    ctx.add(_scope_card(meta))
    h2(
        ctx,
        flow,
        "02",
        "Methodology & boundaries",
        315.95,
        tc=[("03\tApproach and methodology", 1)],
    )
    text_para(ctx, flow, "BodyText", meta["methodology"], 342.5, 3, 13.4, right=-4)
    text_para(
        ctx,
        flow,
        "BodyText",
        meta["limitations"],
        402.25,
        2,
        13.3,
        Font(size=9.5, color=INK2),
        right=30,
    )
    text_para(ctx, flow, "CardLabel", "REFERENCED GUIDANCE", 455.6, 1, 10, Font(color=BLUE))
    add_after(ctx.body[-1], flow.block(473.6 - 15.2, 57))
    ctx.add(_guidance(ctx))
    h2(ctx, flow, "03", "Severity interpretation", 554.95)
    add_after(ctx.body[-1], flow.block(571, 153))
    ctx.add(_severity_table())


SCOPE_ROWS = (
    (
        185.0,
        ("TARGET APPLICATION", "application", 206.55, 223.95, Font(size=12.5)),
        ("TEST TYPE", "test_type", 206.55, 223.85, Font(size=11.5)),
    ),
    (
        235.0,
        ("APPLICATION URL", "url", 254.55, 270.75, Font(LATO_MED, 10, BLUE)),
        ("ASSESSMENT WINDOW", "window", 254.55, 271.75, Font(size=10.5)),
    ),
)


def _scope_card(meta: dict) -> object:
    """Engagement scope card: two rows of (label, value) pairs in two columns."""
    rows = []
    for top, *pairs in SCOPE_ROWS:
        cells = []
        for label, key, label_base, value_base, font in pairs:
            content = cell_paras(
                top,
                [
                    ("CardLabel", label, label_base, None),
                    ("CardValue", meta[key], value_base, font),
                ],
            )
            cells.append(Cell(content, shading=PANEL, pad=(0, 13.1, 0, 4) if not cells else None))
        rows.append(Row(cells, height=50))
    return table([290, 236], rows)


def _guidance(ctx: Ctx) -> object:
    num = ctx.numbering.new_list(BULLET_ABSTRACT)
    fmt = Para(line=19, num=(num, 0), left=13.1, hanging=11.1)
    font = Font(LATO_MED, 8.5, INK2)
    rows = []
    for left_text, right_text in GUIDANCE:
        cells = [Cell([para([run(left_text, font)], fmt)])]
        cells.append(Cell([para([run(right_text, font)], fmt)] if right_text else [para()]))
        rows.append(Row(cells, height=19))
    return table([267, 259], rows)


def _severity_table() -> object:
    rows: list[Row] = []
    for index, (label, sev, text) in enumerate(SEVERITY_ROWS):
        top = 571 + 31 * index
        fill = PANEL if index % 2 == 0 else None
        label_p = cell_paras(
            top, [("CardLabel", label, top + 18.65, Font(LATO_HEAVY, 8.5, SEV_A[sev]))]
        )
        text_p = cell_paras(top, [("CardValue", text, top + 18.6, Font(LATO, 9, INK))])
        cells = [
            Cell(label_p, shading=fill, borders={"left": (4, SEV_A[sev])}, pad=(0, 7.1, 0, 0)),
            Cell(text_p, shading=fill),
        ]
        if index:
            rows.append(gap_row(2, 2))
        rows.append(Row(cells, height=29))
    return table([147, 379], rows)


# ---------------------------------------------------------------- page 5


def coverage(ctx: Ctx) -> None:
    meta, flow = ctx.meta, Flow(72)
    h1(ctx, flow, "Coverage & tooling", 107.15)
    kicker(ctx, flow, "03  /  TESTING CHECKLIST & UTILITIES", 131.65)
    text_para(ctx, flow, "Lead", meta["coverage_lead"], 161.5, 2, 13.2)
    add_after(ctx.body[-1], flow.block(194, 291))
    ctx.add(_owasp_table())
    h2(ctx, flow, "04", "Tools and validation techniques", 522.95, tc=[("04\tTesting tools", 1)])
    text_para(ctx, flow, "Lead", meta["tools_lead"], 543.65, 1, 12, Font(size=9))
    add_after(ctx.body[-1], flow.block(560, 161))
    ctx.add(_tools_grid())


CHIP_TOP = 4.0  # chip sits 4 pt below the row top (spacing, not a cell margin: LibreOffice
# applies the largest cell top margin to every cell of the row)


def _chip(done: bool) -> object:
    fmt = Para(style="Chip", before=CHIP_TOP)
    if done:
        return para([run("DONE", Font(position=2.6))], fmt.with_(shading="DDF5EC"))
    font = Font(LATO_HEAVY, 8, INK2, position=2.4)
    return para([run("N/A", font)], fmt.with_(shading="E6EBEF"))


def _owasp_table() -> object:
    head_font = Font(LATO_HEAVY, 8.5, "FFFFFF")
    head = [
        Cell(
            cell_paras(194, [("CardLabel", "ID", 212.6, head_font)]),
            shading=BANNER,
            pad=(0, 11.1, 0, 0),
        ),
        Cell(
            cell_paras(
                194,
                [("CardLabel", "OWASP TOP 10 (2025)\tEVALUATION", 212.6, head_font)],
                Para(tabs=((480 - 17.3, "right"),)),
            ),
            shading=BANNER,
            span=2,
        ),
    ]
    rows = [Row(head, height=31)]
    for index, (code, name, done) in enumerate(OWASP):
        top = 225 + 26 * index
        fill = PANEL if index % 2 == 0 else None
        cells = [
            Cell(
                cell_paras(top, [("CardLabel", code, top + 16.6, Font(LATO_HEAVY, 8.5, BLUE))]),
                shading=fill,
                pad=(0, 11.1, 0, 0),
            ),
            Cell(
                cell_paras(top, [("CardValue", name, top + 16.65, Font(LATO_MED, 9, INK))]),
                shading=fill,
            ),
            Cell([_chip(done)], shading=fill, pad=(0, 0, 0, 16)),
        ]
        rows.append(Row(cells, height=26))
    return table([46, 415, 65], rows)


def _tool_cells(number: int, name: str, pad_top: float) -> list[Cell]:
    small = number == 15
    chip = para(
        [run(f"{number:02d}", Font(LATO_HEAVY, 7, BLUE))],
        Para(line=16, align="center", shading="DFEDF3"),
    )
    label = para([run(name, Font(LATO_MED, 7 if small else 8.5, INK))], Para(line=16))
    return [
        Cell([chip], shading=PANEL, pad=(pad_top, 12, 0, 0)),
        Cell([label], shading=PANEL, pad=(pad_top, 6, 0, 0)),
    ]


TOOL_COLUMNS = (176.0, 176.0, 174.0)
TOOLS_PER_COLUMN = 5


def _tool_column(column: int, width: float) -> object:
    """One column of the tools card (numbered top to bottom, like the PDF)."""
    rows = []
    for index in range(TOOLS_PER_COLUMN):
        number = column * TOOLS_PER_COLUMN + index + 1
        pad_top = 16 if index == 0 else 0
        height = 27 + (16 if index == 0 else 0) + (10 if index == TOOLS_PER_COLUMN - 1 else 0)
        rows.append(Row(_tool_cells(number, TOOLS[number - 1], pad_top), height=height))
    return table([28, width - 28], rows)


def _tools_grid() -> object:
    cells = [
        Cell(nested(_tool_column(col, width)), shading=PANEL)
        for col, width in enumerate(TOOL_COLUMNS)
    ]
    return table(list(TOOL_COLUMNS), [Row(cells, height=161)])


# ---------------------------------------------------------------- page 6

PRIORITIES = (
    ("PRIORITY 01", "CRITICAL", SEV_A["Critical"], "priority_1", "priority_1_action"),
    ("PRIORITY 02", "HIGH", SEV_A["High"], "priority_2", "priority_2_action"),
    ("PRIORITY 03", "MEDIUM / LOW / INFO", BLUE, "priority_3", "priority_3_action"),
)


def remediation(ctx: Ctx) -> None:
    meta, flow = ctx.meta, Flow(72)
    h1(ctx, flow, "Remediation priorities", 107.15)
    kicker(ctx, flow, "MANAGEMENT CLOSING SUMMARY", 131.65)
    text_para(ctx, flow, "Lead", meta["remediation_lead"], 165.75, 2, 14)
    add_after(ctx.body[-1], flow.block(222, 98))
    ctx.add(_risk_banner(meta))
    h2(ctx, flow, "01", "Recommended remediation sequence", 354.95)
    add_after(ctx.body[-1], flow.block(377, 314))
    ctx.add(_priority_cards(meta))


RISK_TOP, RISK_INNER_TOP = 222.0, 250.0


def _risk_inner(meta: dict) -> object:
    """Nested row of the risk banner: priority count | 'CRITICAL / HIGH' and its note."""
    count = cell_paras(
        RISK_INNER_TOP, [("CardValue", meta["priority"], 297.4, Font(LATO_BLACK, 45, MINT))]
    )
    note = cell_paras(
        RISK_INNER_TOP,
        [
            ("CardLabel", "CRITICAL / HIGH", 272.75, Font(LATO_HEAVY, 10, "FFFFFF")),
            ("CardValue", meta["priority_note"], 291.65, Font(LATO_MED, 9.5, "C1D5DE")),
        ],
    )
    cells = [
        Cell(count, shading=BANNER, pad=(0, 13.1, 0, 0)),
        Cell(note, shading=BANNER, pad=(0, 0.1, 0, 0)),
    ]
    return table([104, 274], [Row(cells, height=70)])


def _risk_banner(meta: dict) -> object:
    """Closing risk profile: label over (04 | critical / high note), then 'other findings'."""
    label = cell_paras(
        RISK_TOP,
        [("CardLabel", "CLOSING RISK PROFILE", 245.65, Font(LATO_HEAVY, 8.5, "9AC0C7"))],
        Para(left=17.1),
    )
    add_after(label[-1], RISK_INNER_TOP - 247.65)
    other = cell_paras(
        RISK_TOP,
        [
            ("CardValue", meta["other"], 268.05, Font(LATO_BLACK, 28, "FFFFFF")),
            ("CardLabel", "OTHER FINDINGS", 291.6, Font(LATO_HEAVY, 8, "A8C4CC")),
        ],
    )
    cells = [
        Cell([*label, *nested(_risk_inner(meta))], shading=BANNER),
        Cell(other, shading=BANNER, pad=(0, 0.1, 0, 0)),
    ]
    return table([378, 148], [Row(cells, height=98)])


CARD_BODY, CARD_ACTION = 70.0, 28.0  # card = body row (label + 2-line text) + action row


def _priority_rows(meta: dict, spec: tuple, top: float) -> list[Row]:
    """One remediation card: label/badge + text row, then the action row (fixed 2-line body)."""
    label, badge, color, text_key, action_key = spec
    flow = Flow(top)
    head = para(
        [run(label, Font(LATO_HEAVY, 8, INK2)), run("\t" + badge, Font(LATO_HEAVY, 8, color))],
        flow.fmt(Para(line=10, tabs=((493.5, "right"),)), top + 17.55),
    )
    body = para(
        [run(meta[text_key], Font(LATO, 9.5, INK))], flow.fmt(Para(line=12.5), top + 35.0, lines=2)
    )
    flow = Flow(top + CARD_BODY)
    action = para(
        [run(meta[action_key], Font(LATO, 9, INK2))], flow.fmt(Para(line=11), top + 79.45)
    )
    accent = {"left": (4, color)}
    pad = (0, 10.1, 0, 12)
    return [
        Row([Cell([head, body], shading=PANEL, borders=accent, pad=pad)], height=CARD_BODY),
        Row([Cell([action], shading=PANEL, borders=accent, pad=pad)], height=CARD_ACTION),
    ]


def _priority_cards(meta: dict) -> object:
    rows: list[Row] = []
    for index, spec in enumerate(PRIORITIES):
        if index:
            rows.append(gap_row(1, 10))
        rows.extend(_priority_rows(meta, spec, 377 + 108 * index))
    return table([526], rows)
