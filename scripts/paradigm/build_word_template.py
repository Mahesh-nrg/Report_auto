"""Build the ParadigmIT VAPT report as a native Word document (deliverable 1).

    python build_word_template.py [OUTPUT.docx]

Default output: report_templates/generated/ParadigmIT_VAPT_Report.docx (next to the source
PDF). The content is the PDF's example content: data/report_content.json (findings and
register, extracted by make_content_json.py) plus the audit-level values in ``example_meta``.
Sections (all 'next page'): 1 = cover (no header/footer, full-bleed art); 2-6 = designed pages
2-6, one section per page (header + footer variant A defined on section 2, inherited by 3-6);
7 = register + findings, pages 7-23 (header + footer variant B, inherited by the rest);
8 = disclaimer (p24); 9 = credentials (p25); 10 = closing page (p26). A page = a section keeps
the measured top spacing of the first paragraph (LibreOffice drops space-before on
page-break-before paragraphs, Word keeps it in both cases).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from docx import Document  # noqa: E402
from docx.document import Document as DocumentType  # noqa: E402
from lxml import etree  # noqa: E402
from ox import Para, el, para  # noqa: E402
from pd_back import closing, credentials, disclaimer  # noqa: E402
from pd_common import Ctx  # noqa: E402
from pd_findings import finding, findings_intro, register  # noqa: E402
from pd_front import cover, executive, inside_report  # noqa: E402
from pd_hdrftr import (  # noqa: E402
    COVER_SETUP,
    VARIANT_A,
    VARIANT_B,
    PageSetup,
    build_footer,
    build_header,
    sect_pr,
)
from pd_meta import example_meta  # noqa: E402
from pd_middle import coverage, framework, remediation  # noqa: E402
from pd_order import order_content  # noqa: E402
from pd_styles import Numbering, install_styles  # noqa: E402

REPO = HERE.parent.parent
DEFAULT_OUT = REPO.parent / "report_templates" / "generated" / "ParadigmIT_VAPT_Report.docx"
CONTENT = HERE / "data" / "report_content.json"
DESIGNED_SETUP = PageSetup(top=72, bottom=56, header=30, footer=20)
LEGACY_SETUP = PageSetup(top=72, bottom=71, header=30, footer=20)
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _end_section(ctx: Ctx, sect: etree._Element) -> None:
    """Attach a section break to the last paragraph, or to a 1 pt carrier after a table."""
    last = ctx.body[-1]
    if last.tag != f"{{{W_NS}}}p":
        last = ctx.add(para(fmt=Para(line=1, after=0)))
    ppr = last.find(f"{{{W_NS}}}pPr")
    spacing = ppr.find(f"{{{W_NS}}}spacing")
    if spacing is not None:
        # LibreOffice subtracts the last paragraph's space-after from the next section's
        # first space-before; at a page end the value has no visible effect in Word.
        spacing.set(f"{{{W_NS}}}after", "0")
    ppr.append(sect)


def _designed_pages(ctx: Ctx) -> None:
    """Pages 2-6: one section per page (section starts keep the measured top spacing)."""
    builders = [inside_report, executive, framework, coverage, remediation]
    for index, build in enumerate(builders):
        build(ctx)
        refs = (
            (build_header(ctx.doc, _logo(), VARIANT_A), build_footer(ctx.doc, VARIANT_A))
            if index == 0
            else (None, None)
        )
        if index < len(builders) - 1:
            _end_section(ctx, sect_pr(DESIGNED_SETUP, *refs))
        else:
            _end_section(ctx, sect_pr(DESIGNED_SETUP))


def _legacy_pages(ctx: Ctx) -> etree._Element:
    """Pages 7-26: register + findings flow, then disclaimer, credentials and closing pages.

    Returns the final (body) section properties; the header/footer variant B is attached to
    the register/findings section and inherited by the following sections.
    """
    register(ctx)
    findings_intro(ctx)
    for index, item in enumerate(ctx.content["findings"]):
        finding(ctx, item, first=index == 0)
    _end_section(
        ctx,
        sect_pr(
            LEGACY_SETUP,
            build_header(ctx.doc, _logo(), VARIANT_B),
            build_footer(ctx.doc, VARIANT_B),
        ),
    )
    disclaimer(ctx)
    _end_section(ctx, sect_pr(LEGACY_SETUP))
    credentials(ctx)
    _end_section(ctx, sect_pr(LEGACY_SETUP))
    closing(ctx)
    return sect_pr(LEGACY_SETUP)


def _settings(doc: DocumentType) -> None:
    settings = doc.settings.element
    compat = settings.find(f"{{{W_NS}}}compat")
    if compat is None:
        compat = el("w:compat")
        settings.append(compat)
    for child in list(compat):
        compat.remove(child)
    compat.append(
        el(
            "w:compatSetting",
            {
                "w:name": "compatibilityMode",
                "w:uri": "http://schemas.microsoft.com/office/word",
                "w:val": "15",
            },
        )
    )


def _properties(doc: DocumentType) -> None:
    props = doc.core_properties
    props.title = "Vulnerability Assessment & Penetration Testing"
    props.subject = "VAPT report - Example Web Application"
    props.author = "ParadigmIT Cybersecurity"
    props.keywords = "VAPT, penetration test, ParadigmIT"
    props.comments = "Word conversion of the ParadigmIT VAPT report design (example content)."


def build_document(meta: dict | None = None, content: dict | None = None) -> DocumentType:
    doc = Document()
    install_styles(doc)
    body = doc.element.body
    for child in list(body):
        body.remove(child)
    ctx = Ctx(
        doc,
        Numbering(doc),
        content or order_content(json.loads(CONTENT.read_text(encoding="utf-8"))),
        meta or example_meta(),
    )
    cover(ctx)
    _end_section(ctx, sect_pr(COVER_SETUP))
    _designed_pages(ctx)
    final = _legacy_pages(ctx)
    for node in ctx.body:
        body.append(node)
    body.append(final)
    etree.cleanup_namespaces(doc.element)
    _settings(doc)
    _properties(doc)
    return doc


def _logo() -> Path:
    return HERE / "assets" / "logo_header.png"


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    build_document().save(str(out))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
