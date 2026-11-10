"""Structure of the ParadigmIT VAPT Word report (deliverable 1): styles, fonts, headings, TOC,
tables, images, header/footer fields, numbering, sections and the repo's layout validator."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from docx import Document
from paradigm_word_helpers import SCRIPTS, W, build, part, part_names

pytest.importorskip("lxml")
if not (SCRIPTS / "assets" / "poc_001.png").exists():
    pytest.skip("Paradigm generator assets are absent", allow_module_level=True)

FINDINGS = 19
DETAIL_LABELS = (
    "S. No.",
    "Name of Vulnerability",
    "Vulnerable Point / Error Location",
    "CVE / CWE",
    "CVSS Score",
    "Severity",
    "Description",
    "Proof of Concept",
    "Steps to Reproduce",
    "Recommendations",
    "References",
)
GROUP_SUB_LABELS = ("Vulnerable Location", "Vulnerable Path / URL", "Vulnerable Parameter")
STYLE_FONTS = {
    "Title": ("Lato Black", 76),
    "Heading1": ("Lato Black", 58),
    "Heading2": ("Lato", 26),
    "Heading3": ("Carlito", 22),
    "Kicker": ("Lato Heavy", 18),
    "BodyText": ("Lato", 20),
    "TOC1": ("Lato", 24),
    "TableLabel": ("Carlito", 18),
    "TableValue": ("Carlito", 18),
    "Caption": ("Carlito", 15),
    "Chip": ("Lato Heavy", 15),
    "Header": ("Lato Heavy", 17),
    "Footer": ("Lato", 14),
    "RegisterHeading": ("Noto Serif", 32),
    "ClosingTitle": ("Noto Serif", 74),
}


@pytest.fixture(scope="module")
def docx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build(tmp_path_factory.mktemp("paradigm") / "ParadigmIT_VAPT_Report.docx")


def _style(styles, sid: str):
    return styles.find(f"{W}style[@{W}styleId='{sid}']")


def _text(node) -> str:
    return "".join(node.itertext())


def test_python_docx_reopens_and_package_is_clean(docx: Path) -> None:
    doc = Document(str(docx))
    assert len(doc.sections) == 10
    for name in part_names(docx):
        if name.endswith(".rels"):
            ids = [rel.get("Id") for rel in part(docx, name)]
            assert len(ids) == len(set(ids)), name


@pytest.mark.parametrize("sid", sorted(STYLE_FONTS))
def test_styles_define_font_family_and_size(docx: Path, sid: str) -> None:
    style = _style(part(docx, "word/styles.xml"), sid)
    assert style is not None, sid
    family, half_points = STYLE_FONTS[sid]
    assert style.find(f"{W}rPr/{W}rFonts").get(f"{W}ascii") == family
    assert style.find(f"{W}rPr/{W}sz").get(f"{W}val") == str(half_points)


def test_severity_character_styles_carry_tokens(docx: Path) -> None:
    styles = part(docx, "word/styles.xml")
    colours = {"Critical": "C00000", "High": "ED7D31", "Medium": "BF9000", "Low": "2E75B6"}
    for sev, colour in colours.items():
        style = _style(styles, f"Severity{sev}")
        assert style.find(f"{W}rPr/{W}color").get(f"{W}val") == colour


def test_headings_use_heading_styles_with_outline(docx: Path) -> None:
    body = part(docx, "word/document.xml")
    titles = [
        _text(p)
        for p in body.iter(f"{W}p")
        if p.find(f"{W}pPr/{W}pStyle[@{W}val='Heading1']") is not None
    ]
    for wanted in ("Inside this report", "Executive overview", "Assessment framework"):
        assert any(wanted in title for title in titles), wanted
    findings = [
        _text(p)
        for p in body.iter(f"{W}p")
        if p.find(f"{W}pPr/{W}pStyle[@{W}val='Heading3']") is not None
    ]
    assert len(findings) == FINDINGS
    assert findings[0].startswith("Finding 001")


def test_index_is_a_toc_field_over_tc_entries(docx: Path) -> None:
    body = part(docx, "word/document.xml")
    instructions = [node.text.strip() for node in body.iter(f"{W}instrText")]
    assert any(code.startswith("TOC \\f p") for code in instructions)
    level_one = [code for code in instructions if code.startswith("TC ") and "\\l 1" in code]
    assert len(level_one) == 8
    assert "Detailed technical findings" in _text(body)


def test_detail_tables_have_the_pdf_labels(docx: Path) -> None:
    doc = Document(str(docx))
    details = [t for t in doc.tables if len(t.columns) == 2 and t.rows[0].cells[0].text == "S. No."]
    assert len(details) == FINDINGS
    for table in details:
        assert tuple(row.cells[0].text for row in table.rows) == DETAIL_LABELS
        group_cell = table.rows[2].cells[1]
        assert len(group_cell.tables) == 1
        sub = group_cell.tables[0]
        assert tuple(row.cells[0].text for row in sub.rows) == GROUP_SUB_LABELS


def test_register_table_repeats_its_header(docx: Path) -> None:
    doc = Document(str(docx))
    register = next(
        t for t in doc.tables if t.rows[0].cells[0].text == "S. No." and len(t.columns) == 5
    )
    assert len(register.rows) == FINDINGS + 1
    assert register.rows[0]._tr.find(f"{W}trPr/{W}tblHeader") is not None


def test_steps_and_recommendations_are_word_numbering(docx: Path) -> None:
    body = part(docx, "word/document.xml")
    lists = {
        p.find(f"{W}pPr/{W}numPr/{W}numId").get(f"{W}val")
        for p in body.iter(f"{W}p")
        if p.find(f"{W}pPr/{W}pStyle[@{W}val='TableList']") is not None
    }
    assert len(lists) == 2 * FINDINGS  # every list restarts at 1


def test_images_keep_pixel_aspect(docx: Path) -> None:
    doc = Document(str(docx))
    pocs = [s for s in doc.inline_shapes if s._inline.docPr.get("name", "").startswith("PoC")]
    assert len(pocs) == FINDINGS
    for shape in pocs:
        image = doc.part.related_parts[shape._inline.graphic.graphicData.pic.blipFill.blip.embed]
        px_w, px_h = image.image.px_width, image.image.px_height
        assert abs(shape.width / shape.height - px_w / px_h) < 0.01


def test_header_footer_have_logo_and_page_field(docx: Path) -> None:
    names = part_names(docx)
    headers = [n for n in names if n.startswith("word/header")]
    footers = [n for n in names if n.startswith("word/footer")]
    assert len(headers) == 2 and len(footers) == 2
    for name in headers:
        assert "PARADIGMIT CYBERSECURITY" in _text(part(docx, name))
        assert part(docx, name).find(f".//{W}drawing") is not None
    for name in footers:
        codes = [node.text.strip() for node in part(docx, name).iter(f"{W}instrText")]
        assert "PAGE" in codes


def test_cover_section_has_no_header_and_letter_pages(docx: Path) -> None:
    doc = Document(str(docx))
    cover = doc.sections[0]._sectPr
    assert cover.find(f"{W}headerReference") is None
    for section in doc.sections:
        assert round(section.page_width.pt) == 612 and round(section.page_height.pt) == 792


def test_no_spacer_paragraphs_or_manual_page_breaks(docx: Path) -> None:
    body = part(docx, "word/document.xml").find(f"{W}body")
    for paragraph in body.findall(f"{W}p"):
        has_content = _text(paragraph).strip() or paragraph.find(f".//{W}drawing") is not None
        assert has_content or paragraph.find(f".//{W}sectPr") is not None
    assert not body.findall(f".//{W}br[@{W}type='page']")


def test_layout_validator_reports_no_blank_pages(docx: Path) -> None:
    layout = pytest.importorskip("pwndoc_ai.docx_layout")
    report = layout.inspect_layout(io.BytesIO(docx.read_bytes()).getvalue())
    assert report.stats["est_blank_pages"] == 0
    assert report.stats["empty_sections"] == 0
    assert report.stats["empty_paragraphs"] == 0
    assert not [i for i in report.issues if i.severity == "error"]
