"""Disclaimer (page 24), credentials (page 25) and the closing 'THANK YOU' page (26)."""

from __future__ import annotations

from ox import Cell, Font, Para, Row, inline_picture, para, run, table
from pd_common import Ctx, h1, tc_entry, text_para
from pd_flow import Flow, add_after
from pd_styles import CRED_PANEL, HEAD_BLUE, LATO, LATO_BLACK, LATO_HEAVY

QUALIFICATIONS = (
    ("cred_cissp.png", "CISSP badge", "Information Systems\nSecurity Professional"),
    ("cred_cisa.png", "CISA badge", "Information Systems\nAuditor"),
    ("cred_oscp.png", "OSCP badge", "Offensive Security\nCertified Professional"),
    ("cred_ceh.png", "CEH badge", "Certified\nEthical Hacker"),
)
ASSURANCE = (
    ("iso_27001.png", "ISO 27001 mark", "Information security\nmanagement"),
    ("iso_9001.png", "ISO 9001 mark", "Quality\nmanagement"),
    ("iso_47001.png", "ISO 47001 mark", "Designation\nto verify"),
    ("aicpa_soc2.png", "AICPA SOC 2 mark", "Service Organization\nControl attestation"),
    ("cmmi_l5.png", "CMMI Level 5 mark", "Capability maturity\nLevel 5 appraisal"),
)
CRED_TEXT = "657788"


def disclaimer(ctx: Ctx) -> None:
    flow = Flow(72)
    runs = [run("07  /  DISCLAIMER", Font(size=15))]
    runs.extend(tc_entry("07\tDisclaimer", 1))
    fmt = flow.fmt(Para(style="SectionKicker", page_break_before=False, line=18), 96.3)
    ctx.add(para(runs, fmt))
    base = 121.95
    for text, lines in ctx.meta["disclaimer"]:
        fmt = flow.fmt(Para(style="Disclaimer"), base, lines=lines, after=10)
        ctx.add(para([run(text)], fmt))
        base += (lines - 1) * 16.1 + 26.1


def credentials(ctx: Ctx) -> None:
    meta, flow = ctx.meta, Flow(72)
    h1(
        ctx,
        flow,
        "The expertise behind\nthe assessment",
        110.4,
        size=33,
        line=38,
        lines=2,
        tc=[("08\tCredentials & assurance", 1)],
    )
    text_para(
        ctx,
        flow,
        "Kicker",
        "08   /   PROFESSIONAL ASSURANCE & CREDENTIALS",
        184.0,
        1,
        12,
        Font(LATO_HEAVY, 10, HEAD_BLUE, spacing=0.38),
    )
    text_para(ctx, flow, "Lead", meta["credentials_lead"], 210.4, 3, 13, Font(LATO, 11, CRED_TEXT))
    text_para(
        ctx,
        flow,
        "Kicker",
        "PROFESSIONAL QUALIFICATIONS",
        272.35,
        1,
        12.5,
        Font(LATO_HEAVY, 10.5, HEAD_BLUE, spacing=0.47),
    )
    add_after(ctx.body[-1], flow.block(282, 130))
    ctx.add(_badge_panel(ctx, QUALIFICATIONS, 76, (15.95, 390.0 - 282, 10.0, 8.5), 130))
    text_para(
        ctx,
        flow,
        "Heading2",
        "Organisational assurance",
        446.4,
        1,
        25,
        Font(LATO_BLACK, 21),
        keep_next=True,
    )
    text_para(ctx, flow, "Lead", meta["assurance_lead"], 468.0, 1, 13, Font(LATO, 11, CRED_TEXT))
    add_after(ctx.body[-1], flow.block(482, 128))
    ctx.add(_badge_panel(ctx, ASSURANCE, 72, (13.95, 582.5 - 482, 9.4, 8), 128))


def _badge_cell(
    ctx: Ctx, spec: tuple[str, str, str], size: float, geometry: tuple[float, float, float, float]
) -> Cell:
    image_top, caption_base, pitch, font_size = geometry
    name, alt, caption = spec
    picture = inline_picture(ctx.image(name), size, size, alt, alt)
    flow = Flow(0)
    image = para([picture], Para(align="center", before=image_top, line=1, line_rule="auto"))
    flow.y = image_top + size
    cap_fmt = flow.fmt(Para(align="center", line=pitch), caption_base, lines=2)
    text = para([run(caption, Font(LATO, font_size, CRED_TEXT))], cap_fmt)
    return Cell([image, text], shading=CRED_PANEL)


def _badge_panel(
    ctx: Ctx, specs: tuple, size: float, geometry: tuple[float, float, float, float], height: float
) -> object:
    cells = [_badge_cell(ctx, spec, size, geometry) for spec in specs]
    width = 526 / len(specs)
    return table([width] * len(specs), [Row(cells, height=height)])


def closing(ctx: Ctx) -> None:
    flow = Flow(72)
    fmt = flow.fmt(Para(style="ClosingTitle", page_break_before=False), 266.55)
    ctx.add(para([run("THANK YOU")], fmt))
    ctx.add(para([run("ParadigmIT Cybersecurity")], flow.fmt(Para(style="ClosingSubtitle"), 321.2)))
    ctx.add(
        para(
            [run(ctx.meta["closing_line"], Font(LATO, 12, "566E82"))],
            flow.fmt(Para(align="center", line=15), 359.95),
        )
    )
    ctx.add(
        para(
            [run("PRIVATE & CONFIDENTIAL", Font(LATO, 9, "64788A", bold=True))],
            flow.fmt(Para(align="center", line=11), 433.55),
        )
    )
