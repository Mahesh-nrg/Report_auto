"""Label-driven reading of the detail and summary tables."""

from __future__ import annotations

import pytest

pytest.importorskip("lxml")

from reader_fixtures import (  # noqa: E402
    BULLET_NUM,
    DECIMAL_NUM,
    cell,
    detail_table,
    p,
    para,
    picture,
    row,
    run,
    summary_table,
    table,
)

from pwndoc_ai.docx_engine.xmlutil import parse_xml  # noqa: E402
from pwndoc_ai.docx_import import findings, limits  # noqa: E402
from pwndoc_ai.docx_import.html import Para  # noqa: E402
from pwndoc_ai.docx_import.reader_xml import Counter, ImageRef, rows  # noqa: E402

NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
)
NUMBERING = {BULLET_NUM: {0: "bullet", 1: "bullet"}, DECIMAL_NUM: {0: "decimal", 1: "lowerLetter"}}


def element(xml: str):
    return parse_xml(f"<w:body {NS}>{xml}</w:body>".encode())[0]


def detail(**kwargs) -> findings.RawFinding:
    base = {"ident": "003", "title": "Unauthorized Downloads"}
    base.update(kwargs)
    ident, title = base.pop("ident"), base.pop("title")
    return findings.read_detail(
        element(detail_table(ident, title, **base)), 0, NUMBERING, Counter()
    )


def test_detail_table_fields_by_label():
    found = detail(
        location="Videos in the application",
        urls=[p("https://a/video.m3u8?token=<JWT>  https://b/x"), p("https://c/y")],
        parameter="token",
        cwe="CWE-284",
        cvss="8.8",
        severity="High",
        description=[p("First"), p("During testing it was observed")],
        remediation=[p("Fix", bold=True), p("a", num=BULLET_NUM)],
        references=[p("https://owasp.org/x"), p("- https://cwe.mitre.org/y"), p("undefined")],
    )
    assert found.origin == "detail" and found.source_id == "003"
    assert found.title == "Unauthorized Downloads"
    assert found.location == "Videos in the application" and found.parameter == "token"
    assert found.urls == ("https://a/video.m3u8?token=<JWT>", "https://b/x", "https://c/y")
    assert (found.cwe, found.cvss_score, found.cvss_vector, found.severity) == (
        "CWE-284",
        "8.8",
        "",
        "High",
    )
    assert found.description == "<p>First</p><p>During testing it was observed</p>"
    assert found.observation == ""
    assert found.remediation == "<p><strong>Fix</strong></p><ul><li><p>a</p></li></ul>"
    assert found.references == ("https://owasp.org/x", "https://cwe.mitre.org/y")


def test_blank_placeholders_stay_blank():
    found = detail(location="undefined", parameter="N/A", cwe="-", cvss="", severity="undefined")
    assert (found.location, found.parameter, found.cwe, found.cvss_score, found.severity) == (
        "",
    ) * 5


def test_poc_label_is_dropped_and_steps_are_kept():
    found = detail(
        poc=[p("Steps to Reproduce:"), p("one", num=DECIMAL_NUM), p("two", num=DECIMAL_NUM)]
    )
    texts = [item.text for item in found.poc if isinstance(item, Para) and item.text.strip()]
    assert texts == ["Steps to Reproduce:", "one", "two"]


def test_poc_label_with_text_on_the_same_line_keeps_the_text():
    xml = table(
        row(cell(p("S. No.")), cell(p("1"), span=2)),
        row(cell(p("Name of Vulnerability")), cell(p("T"), span=2)),
        row(cell(p("Severity")), cell(p("Low"), span=2)),
        row(cell(p("Description")), cell(p("d"), span=2)),
        row(cell(p("Proof of concept: open the page"), span=3)),
    )
    found = findings.read_detail(element(xml), 0, {}, Counter())
    assert [i.text for i in found.poc if isinstance(i, Para)] == ["open the page"]


def test_figures_take_the_caption_after_them_and_the_caption_paragraph_goes():
    found = detail(
        poc=[p("intro"), picture("rId9"), p(""), p("Fig: Burp request"), picture("rId10")]
    )
    figures = [i for i in found.poc if isinstance(i, findings.Figure)]
    assert [(f.rid, f.caption, f.captioned) for f in figures] == [
        ("rId9", "Burp request", True),
        ("rId10", "", False),
    ]
    assert not any(isinstance(i, Para) and "Fig" in i.text for i in found.poc)


def test_pictures_in_other_cells_become_extra_figures():
    found = detail(description=[p("text"), picture("rId4"), p("Figure 1: In description")])
    assert found.description == "<p>text</p>"
    assert [(f.rid, f.caption) for f in found.figures] == [("rId4", "In description")]


def test_caption_above_is_used_when_none_follows():
    items = findings.pair_figures([Para("Figure 2: Above"), ImageRef("rId1", 1), Para("after")])
    assert isinstance(items[0], findings.Figure) and items[0].caption == "Above"
    assert items[1].text == "after"


def test_list_paragraph_is_never_a_caption():
    items = findings.pair_figures([ImageRef("rId1", 1), Para("Figure 1: item", "ul")])
    assert items[0].captioned is False and len(items) == 2


@pytest.mark.parametrize(
    ("text", "vector", "score"),
    [
        ("8.8", "", "8.8"),
        ("10.0", "", "10.0"),
        ("7.50 (High)", "", "7.5"),
        (
            "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
            "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
            "",
        ),
        (
            "CVSS:3.0/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N 6.1",
            "CVSS:3.0/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N",
            "6.1",
        ),
        ("CVSS:3.1/AV:N/AC:L", "", ""),
        ("undefined", "", ""),
        ("11", "", ""),
        ("9.85", "", ""),
    ],
)
def test_cvss_is_taken_only_as_printed(text, vector, score):
    assert findings.parse_cvss(text) == (vector, score)


def test_one_line_fields_are_capped_and_flagged(monkeypatch):
    monkeypatch.setattr(limits, "MAX_LINE_CHARS", 10)
    found = detail(location="x" * 50, urls="https://" + "y" * 50)
    assert found.location == "x" * 10 and len(found.urls[0]) == 10
    assert set(found.truncated) == {"location", "urls"}


def test_rich_field_over_the_cap_is_cut_at_a_block_boundary(monkeypatch):
    monkeypatch.setattr(limits, "MAX_RICH_CHARS", 40)
    found = detail(description=[p("a" * 10), p("b" * 10), p("c" * 10)])
    assert found.description == "<p>" + "a" * 10 + "</p>" + "<p>" + "b" * 10 + "</p>"
    assert "description" in found.truncated


def test_synonym_labels_from_another_template():
    xml = table(
        row(cell(p("Finding ID")), cell(p("VULN-7"))),
        row(cell(p("Title")), cell(p("Open redirect"))),
        row(cell(p("Risk Rating")), cell(p("Moderate"))),
        row(cell(p("Affected URL")), cell(p("https://x/r?u="))),
        row(cell(p("Mitigation")), cell(p("Validate targets"))),
        row(cell(p("Observation")), cell(p("Seen on login"))),
    )
    grid = rows(element(xml))
    assert findings.is_detail_table(grid)
    found = findings.read_detail(element(xml), 0, {}, Counter())
    assert (found.source_id, found.title, found.severity) == ("7", "Open redirect", "Medium")
    assert found.urls == ("https://x/r?u=",) and found.remediation == "<p>Validate targets</p>"
    assert found.observation == "<p>Seen on login</p>"


def test_the_first_row_with_a_label_wins():
    xml = table(
        row(cell(p("Name of Vulnerability")), cell(p("First"))),
        row(cell(p("Title")), cell(p("Second"))),
        row(cell(p("Severity")), cell(p("Low"))),
        row(cell(p("Description")), cell(p("d"))),
    )
    assert findings.read_detail(element(xml), 0, {}, Counter()).title == "First"


def test_summary_table_rows():
    xml = summary_table(
        (
            "001",
            "URL:\nhttps://a/x  https://a/y\nParameter: \n\u201cemail\u201d\nLocation: \nLogin page",
            "Full Account Takeover",
            "",
        ),
        (
            "006",
            "URL: https://blog/xmlrpc.php\nParameter: N/A\nLocation: XMLRPC page",
            "XMLRPC Enumeration",
            "CWE-200",
        ),
        ("", "", "", ""),
    )
    grid = rows(element(xml))
    assert findings.is_summary_table(grid) and not findings.is_detail_table(grid)
    found = findings.read_summary(element(xml), 5000)
    assert [f.source_id for f in found] == ["001", "006"]
    first, second = found
    assert first.origin == "summary" and first.position == 5000
    assert first.urls == ("https://a/x", "https://a/y")
    assert (first.parameter, first.location) == ("\u201cemail\u201d", "Login page")
    assert (second.parameter, second.location, second.cwe) == ("", "XMLRPC page", "CWE-200")


def test_summary_blob_with_a_soft_break_before_the_label():
    xml = table(
        row(cell(p("S. No.")), cell(p("Location")), cell(p("Name of the vulnerability"))),
        row(
            cell(p("2")), cell(para(run("URL:\nhttp://h/u\nParameter: \nfile"))), cell(p("Upload"))
        ),
    )
    found = findings.read_summary(element(xml), 0)
    assert found[0].urls == ("http://h/u",) and found[0].parameter == "file"


def test_unlabelled_blob_is_read_as_urls():
    assert findings.parse_blob("https://a  https://b") == {"urls": ("https://a", "https://b")}


def test_detail_detection_needs_a_title_and_other_fields():
    assert not findings.is_detail_table(rows(element(table(row(cell(p("Title")), cell(p("x")))))))
    tools = table(
        row(cell(p("S.No")), cell(p("Name of the Tool"))), row(cell(p("1")), cell(p("Burp")))
    )
    assert not findings.is_detail_table(rows(element(tools)))
    assert not findings.is_summary_table(rows(element(tools)))
