"""Running header / footer parts and section properties.

Header (pages 2-26): full-bleed 5 pt navy band, flat tri-colour bar (3 DrawingML rectangles),
a client-brand logo slot on the left, 'PARADIGMIT CYBERSECURITY  /  VAPT' and the ParadigmIT
logo at the right. Footer: a three-segment rule, the confidentiality line and a PAGE field.
Variant A (pages 2-6) has the 7 pt footer and a zero-padded page number, variant B (pages 7-26)
the 9.5 pt footer and plain page numbers.

The client-logo slot defaults to a neutral ``[CLIENT LOGO]`` text placeholder (used by the
literal Word report and the import sample, deliverables 1 and 3b, which have no live company
record to pull a real logo from). ``build_pwndoc_template.py`` overrides it with the raw
docxtemplater image tag ``{%company.logo_small}`` so the generated PwnDoc template substitutes
the client company's own logo (set per-company in the PwnDoc UI) at render time -- see
PwnDoc's bundled ``Template1.docx`` header, which uses the identical tag as plain text in the
header paragraph (docxtemplater-image-module-pwndoc replaces the tag text with an inline
picture; no drawing/anchor wrapper is needed or supported for the tag itself).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from docx.document import Document
from lxml import etree
from ox import (
    Box,
    Font,
    Para,
    ShapeStyle,
    anchored_picture,
    anchored_shape,
    el,
    field_runs,
    para,
    run,
    tw,
)
from pd_styles import BLUE, INK2, INK3, LATO, LATO_HEAVY, MINT, NAVY, TEAL

PAGE_W, PAGE_H = 612.0, 792.0
TRI = (BLUE, TEAL, MINT)
HEADER_LINE = 14.5  # header distance 30 + 0.8 * 14.5 = measured baseline 41.6


@dataclass(frozen=True)
class FooterVariant:
    text: str
    text_size: float
    number_size: float
    number_prefix: str
    header_tracking: float
    line: float
    lift: float  # spacing after the footer line: lifts the baseline to the measured one


VARIANT_A = FooterVariant("CONFIDENTIAL  /  CLIENT SECURITY ASSESSMENT", 7, 8, "0", 0, 10, 3.5)
VARIANT_B = FooterVariant(
    "CONFIDENTIAL   /   SAMPLE VAPT REPORT   /   example.com", 9.5, 11, "", 0.48, 13, 3.2
)


def tricolour(
    x: float, y: float, widths: tuple[float, float, float], h: float, name: str
) -> list[etree._Element]:
    """Three flat rectangles (blue / teal / mint) anchored to the page."""
    runs, left = [], x
    for width, color in zip(widths, TRI, strict=True):
        runs.append(
            anchored_shape(Box(left, y, width, h), ShapeStyle(fill=color), f"{name} {color}")
        )
        left += width
    return runs


def default_client_logo() -> list[etree._Element]:
    """Neutral placeholder run for deliverables with no live company record (1 and 3b)."""
    return [run("[CLIENT LOGO]", Font(LATO_HEAVY, 7.5, INK3, caps=True, spacing=0.4))]


def _header_runs(
    doc_part: object,
    logo: Path,
    variant: FooterVariant,
    client_logo: list[etree._Element] | None = None,
) -> list[etree._Element]:
    rid, _img = doc_part.get_or_add_image(str(logo))  # type: ignore[attr-defined]
    band = anchored_shape(Box(0, 0, PAGE_W, 5), ShapeStyle(fill=NAVY), "Header band")
    logo_run = anchored_picture(
        rid, Box(467, 36.95, 100, 23), "ParadigmIT logo", "ParadigmIT Cybersecurity logo"
    )
    client = client_logo if client_logo is not None else default_client_logo()
    gap = run("   ", Font(LATO_HEAVY, 8.5, INK2))
    text = run(
        "PARADIGMIT CYBERSECURITY  /  VAPT",
        Font(LATO_HEAVY, 8.5, INK2, spacing=variant.header_tracking or None),
    )
    return [band, *tricolour(42, 5, (90, 88, 44), 3, "Header bar"), logo_run, *client, gap, text]


def build_header(
    doc: Document,
    logo: Path,
    variant: FooterVariant,
    client_logo: list[etree._Element] | None = None,
) -> str:
    part, rid = doc.part.add_header_part()
    root = part.element
    for child in list(root):
        root.remove(child)
    runs = _header_runs(part, logo, variant, client_logo)
    # atLeast (not exact): a substituted {%company.logo_small} inline picture can be taller than
    # the measured 14.5 pt baseline (PwnDoc scales it to 37 pt) -- exact would clip it.
    root.append(para(runs, Para(style="Header", line=HEADER_LINE, line_rule="atLeast")))
    return rid


def build_footer(doc: Document, variant: FooterVariant) -> str:
    part, rid = doc.part.add_footer_part()
    root = part.element
    for child in list(root):
        root.remove(child)
    num_font = Font(LATO, variant.number_size, INK2)
    runs = [
        *tricolour(43, 748.5, (122, 119, 55), 1.5, "Footer rule"),
        run(variant.text, Font(LATO, variant.text_size, INK3)),
        run("\t", num_font),
    ]
    if variant.number_prefix:
        runs.append(run(variant.number_prefix, num_font))
    runs.extend(field_runs("PAGE", [run("2", num_font)], num_font))
    root.append(para(runs, Para(style="Footer", line=variant.line, after=variant.lift)))
    return rid


@dataclass(frozen=True)
class PageSetup:
    top: float
    bottom: float
    left: float = 43
    right: float = 43
    header: float = 30
    footer: float = 20


COVER_SETUP = PageSetup(top=0, bottom=0, left=46, right=45, header=0, footer=0)
INNER_SETUP = PageSetup(top=72, bottom=56)


def sect_pr(
    setup: PageSetup, header_rid: str | None = None, footer_rid: str | None = None
) -> etree._Element:
    node = el("w:sectPr")
    if header_rid:
        node.append(el("w:headerReference", {"w:type": "default", "r:id": header_rid}))
    if footer_rid:
        node.append(el("w:footerReference", {"w:type": "default", "r:id": footer_rid}))
    node.append(el("w:type", {"w:val": "nextPage"}))
    node.append(el("w:pgSz", {"w:w": tw(PAGE_W), "w:h": tw(PAGE_H)}))
    node.append(
        el(
            "w:pgMar",
            {
                "w:top": tw(setup.top),
                "w:right": tw(setup.right),
                "w:bottom": tw(setup.bottom),
                "w:left": tw(setup.left),
                "w:header": tw(setup.header),
                "w:footer": tw(setup.footer),
                "w:gutter": "0",
            },
        )
    )
    node.append(el("w:cols", {"w:space": "720"}))
    node.append(el("w:docGrid", {"w:linePitch": "360"}))
    return node
