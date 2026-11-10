"""read_report end to end on synthetic reports: structure, screenshots and strict output."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("lxml")

from reader_fixtures import (  # noqa: E402
    DECIMAL_NUM,
    GIF_1X1,
    cell,
    detail_table,
    gif,
    jpeg,
    p,
    package,
    para,
    picture,
    png,
    report,
    row,
    run,
    summary_table,
    table,
)

from pwndoc_ai.docx_import import limits  # noqa: E402
from pwndoc_ai.docx_import.model import AuditHeader, ImportPlan  # noqa: E402
from pwndoc_ai.docx_import.reader import read_report  # noqa: E402

IMPORT_ID = "0123456789ab4def8123456789abcdef"


def read(data: bytes):
    return read_report(data, file_name="Report.docx")


def strict(result) -> ImportPlan:
    """Everything the reader returns passes the model's strict validation."""
    plan = ImportPlan(
        import_id=IMPORT_ID,
        revision=1,
        file_name="Report.docx",
        file_sha256="0" * 64,
        header=AuditHeader(),
        source_header=result.header,
        findings=result.findings,
        images=result.images,
        issues=result.issues,
        stats=result.stats,
    )
    again = ImportPlan.from_json(plan.to_json())
    assert again == plan
    return again


def codes(result) -> list[str]:
    return [issue.code for issue in result.issues]


def test_a_full_report_reads_into_strict_output():
    findings = [
        detail_table(
            "001",
            "SQL Injection",
            severity="High",
            cvss="8.8",
            poc=[p("one"), picture("rId5"), p("Fig: Login")],
        ),
        detail_table("003", "XSS", severity="Low"),
    ]
    summary = summary_table(
        ("001", "URL:\nhttps://a/login\nParameter: \nid", "SQL Injection", "CWE-89"),
        ("003", "URL:\nhttps://a/search", "XSS", "CWE-79"),
        ("004", "URL:\nhttps://a/x", "Missing detail", ""),
    )
    result = read(report(findings, summary=summary, media={"rId5": png()}))
    strict(result)
    assert [(f.key, f.source_id, f.title, f.origin) for f in result.findings] == [
        ("f001", "001", "SQL Injection", "merged"),
        ("f002", "003", "XSS", "merged"),
        ("f003", "004", "Missing detail", "summary"),
    ]
    first, _, stub = result.findings
    assert first.cwe == "CWE-89" and first.parameter == "id" and first.urls == ("https://a/login",)
    assert [b.kind for b in first.poc] == ["html", "image"] and first.poc[1].image_key == "i001"
    assert stub.flags == ("summary_only",)
    assert result.stats.summary_rows == 3 and result.stats.detail_tables == 2
    assert result.stats.merged == 2 and result.stats.id_gaps == ("002",)
    assert {"id_gaps", "duplicates_merged", "leftover_name", "contacts_absent"} <= set(
        codes(result)
    )
    image = result.images[0]
    assert (image.key, image.finding_key, image.caption, image.include) == (
        "i001",
        "f001",
        "Login",
        True,
    )
    assert result.blobs[0].key == "i001" and result.blobs[0].data == png()


def test_tracked_changes_are_read_accepted():
    title = para(run("Old "), "<w:del w:id='1' w:author='a'><w:r><w:delText>Removed </w:delText></w:r></w:del>",
                 "<w:ins w:id='2' w:author='a'>" + run("Added") + "</w:ins>")  # fmt: skip
    xml = table(
        row(cell(p("S. No.")), cell(p("001"))),
        row(cell(p("Name of Vulnerability")), cell(title)),
        "<w:tr><w:trPr><w:del w:id='3' w:author='a'/></w:trPr>"
        + cell(p("Severity"))
        + cell(p("Critical"))
        + "</w:tr>",
        row(cell(p("Severity")), cell(p("Low"))),
        row(cell(p("Description")), cell(p("d"))),
    )
    finding = read(package(xml)).findings[0]
    assert finding.title == "Old Added" and finding.severity == "Low"


def test_hidden_runs_field_codes_and_alternate_content():
    instr = "<w:r><w:fldChar w:fldCharType='begin'/></w:r><w:r><w:instrText>HYPERLINK \"x\"</w:instrText></w:r>"
    instr += (
        "<w:r><w:fldChar w:fldCharType='separate'/></w:r>"
        + run("shown")
        + "<w:r><w:fldChar w:fldCharType='end'/></w:r>"
    )
    alternate = (
        "<w:r><mc:AlternateContent><mc:Choice Requires='wps'><w:r><w:t>choice</w:t></w:r></mc:Choice>"
        "<mc:Fallback><w:r><w:t>fallback</w:t></w:r></mc:Fallback></mc:AlternateContent></w:r>"
    )
    xml = detail_table(
        "001",
        "T",
        severity="High",
        description=[para(run("visible "), run("secret", hidden=True), instr), para(alternate)],
    )
    finding = read(package(xml)).findings[0]
    assert finding.description == "<p>visible shown</p><p>choice</p>"


def test_content_controls_and_nested_layout_tables_are_read_through():
    inner = detail_table("002", "Inside a layout table", severity="Medium")
    layout = table(row(cell(p("left")), cell(inner)))
    wrapped = (
        "<w:sdt><w:sdtPr/><w:sdtContent>"
        + detail_table("001", "In a control", severity="Low")
        + "</w:sdtContent></w:sdt>"
    )
    result = read(package(wrapped + layout))
    assert [(f.source_id, f.title) for f in result.findings] == [
        ("001", "In a control"),
        ("002", "Inside a layout table"),
    ]


def test_word_numbering_typed_lists_and_breaks_in_the_poc():
    poc = [
        p("Steps:", bold=True),
        p("open", num=DECIMAL_NUM),
        p("send\nthe request", num=DECIMAL_NUM),
        p("1. typed a"),
        p("2. typed b"),
        p('GET /a?x=<uuid>&y="z"'),
    ]
    finding = read(package(detail_table("001", "T", severity="Low", poc=poc))).findings[0]
    assert finding.poc[0].html == (
        "<p><strong>Steps:</strong></p><ol><li><p>open</p></li><li><p>send<br>the request</p></li></ol>"
        "<ol><li><p>typed a</p></li><li><p>typed b</p></li></ol>"
        "<p>GET /a?x=&lt;uuid&gt;&amp;y=&quot;z&quot;</p>"
    )


def test_screenshot_types_come_from_the_bytes_and_flags_follow():
    poc = [
        picture("rId1"), p("Figure 1: Jpeg named png"),
        picture("rId2"), p("Figure 2: Gif"),
        picture("rId3"), p("Figure 3: Placeholder"),
        picture("rId4"), p("Figure 4: Webp"),
        picture("rId5"),
    ]  # fmt: skip
    media = {
        "rId1": jpeg(),
        "rId2": gif(),
        "rId3": GIF_1X1,
        "rId4": b"RIFF\0\0\0\0WEBP" + b"\0" * 300,
        "rId5": png(),
    }
    result = read(package(detail_table("001", "T", severity="Low", poc=poc), media=media))
    strict(result)
    got = [(i.media_type, i.include, i.flags) for i in result.images]
    assert got == [
        ("image/jpeg", True, ()),
        ("image/gif", True, ()),
        ("image/gif", False, ("placeholder",)),
        ("", False, ("unsupported",)),
        ("image/png", True, ("uncaptioned",)),
    ]
    assert {"image_placeholder", "image_unsupported", "image_uncaptioned"} <= set(codes(result))


def test_screenshot_after_a_table_goes_to_the_nearest_finding_and_earlier_ones_are_ignored():
    """Pictures before the first detail table (cover logos) are not screenshots; every later
    one goes to the nearest detail table before it, a Heading 1 in between included (COR-3)."""
    body = (
        picture("rId1")  # cover logo, before any finding: not a screenshot
        + detail_table("001", "First", severity="Low", poc=[p("text")])
        + picture("rId2") + p("Fig: Stray screenshot")
        + detail_table("002", "Second", severity="Low")
        + p("Disclaimer", style="Heading1") + picture("rId3")
    )  # fmt: skip
    media = {"rId1": png(10, 10), "rId2": png(), "rId3": png(20, 20)}
    result = read(package(body, media=media))
    strict(result)
    assert len(result.images) == 2
    image, after_heading = result.images
    assert (image.finding_key, image.caption, image.flags) == (
        "f001",
        "Stray screenshot",
        ("unmatched",),
    )
    assert result.findings[0].poc[-1].image_key == image.key
    assert after_heading.finding_key == "f002" and "unmatched" in after_heading.flags
    assert result.findings[1].poc[-1].image_key == after_heading.key
    assert "image_unmatched" in codes(result)


def test_repeated_figures_are_dropped_with_their_orphan_text():
    poc = [
        p("Steps"),
        picture("rId1"), p("Fig: Example 1"),
        picture("rId2"), p("Fig: Example 2"),
        p("\n\nReferences:\n- "), picture("rId1"), p("Fig: Example 1"),
        p("\n- "), picture("rId2"), p("Fig: Example 2"),
    ]  # fmt: skip
    result = read(
        package(
            detail_table("005", "T", severity="Low", poc=poc), media={"rId1": png(), "rId2": jpeg()}
        )
    )
    strict(result)
    assert result.stats.figures_deduped == 2 and "figures_deduped" in codes(result)
    poc_blocks = result.findings[0].poc
    assert [b.kind for b in poc_blocks] == ["html", "image", "image"]
    assert "References" not in poc_blocks[0].html


def test_same_caption_on_different_pictures_is_flagged():
    poc = [picture("rId1"), p("Fig: Same"), picture("rId2"), p("Fig: Same")]
    result = read(
        package(
            detail_table("001", "T", severity="Low", poc=poc), media={"rId1": png(), "rId2": jpeg()}
        )
    )
    assert [i.flags for i in result.images] == [(), ("duplicate_caption",)]


def test_external_and_missing_pictures_are_never_fetched():
    rels = (
        '<Relationship Id="rIdX" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="https://evil.example/x.png" TargetMode="External"/>'
        '<Relationship Id="rIdY" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="../../etc/passwd"/>'
    )
    poc = [picture("rIdX"), picture("rIdY"), picture("rIdZ"), p("text")]
    result = read(package(detail_table("001", "T", severity="Low", poc=poc), rels=rels))
    # nothing is fetched or read, but each picture is listed as one PwnDoc cannot get (COR-3)
    assert [b.data for b in result.blobs] == [b"", b"", b""]
    assert all(i.flags[0] == "unsupported" and not i.include for i in result.images)
    assert "image_unsupported" in codes(result)


def test_a_picture_in_a_one_line_cell_is_kept_and_its_caption_is_not_read_as_a_value():
    table = detail_table("001", "T", severity="Low", cvss="7.5")
    assert table.count(p("7.5")) == 1
    body = table.replace(p("7.5"), p("7.5") + picture("rId1") + p("Figure 1: CVSS calculator"))
    result = read(package(body, media={"rId1": png()}))
    (image,) = result.images
    assert image.finding_key == "f001" and image.caption == "CVSS calculator"
    assert result.findings[0].cvss_score == "7.5"
    assert result.findings[0].poc[-1].image_key == image.key


def test_oversized_pictures_are_flagged_and_not_kept(monkeypatch):
    monkeypatch.setattr(limits, "MAX_IMAGE_BYTES", 1000)
    poc = [picture("rId1"), p("Fig: big"), picture("rId2"), p("Fig: small")]
    media = {"rId1": png(pad=5000), "rId2": png()}
    result = read(package(detail_table("001", "T", severity="Low", poc=poc), media=media))
    strict(result)
    big, small = result.images
    assert big.flags == ("too_large",) and not big.include and big.media_type == "image/png"
    assert small.include and result.blobs[0].data == b"" and result.blobs[1].data == png()


def test_total_picture_bytes_are_capped(monkeypatch):
    monkeypatch.setattr(limits, "MAX_TOTAL_IMAGE_BYTES", 1000)
    poc = [picture("rId1"), picture("rId2"), picture("rId3")]
    media = {"rId1": png(), "rId2": jpeg(), "rId3": gif()}
    result = read(package(detail_table("001", "T", severity="Low", poc=poc), media=media))
    assert [("too_large" in i.flags) for i in result.images] == [False, False, True]


def test_finding_and_image_caps(monkeypatch):
    many = [detail_table(f"{n:03d}", f"Finding {n}", severity="Low") for n in range(1, 6)]
    monkeypatch.setattr(limits, "MAX_FINDINGS", 4)
    with pytest.raises(Exception) as caught:
        read(package("".join(many)))
    assert getattr(caught.value, "code", "") == "too_many_findings"
    monkeypatch.setattr(limits, "MAX_FINDINGS", 200)
    monkeypatch.setattr(limits, "MAX_IMAGES", 2)
    poc = [picture("rId1"), picture("rId2"), picture("rId3")]
    with pytest.raises(Exception) as caught:
        read(
            package(
                detail_table("001", "T", poc=poc),
                media={"rId1": png(), "rId2": jpeg(), "rId3": gif()},
            )
        )
    assert getattr(caught.value, "code", "") == "too_many_images"


def test_huge_cells_are_cut_and_still_valid():
    huge = [p("word " * 30_000)] + [p(f"line {i}") for i in range(3000)]
    poc = [p("x" * 15_000) for _ in range(30)]
    result = read(
        package(detail_table("001", "T" * 1000, severity="Low", description=huge, poc=poc))
    )
    strict(result)
    finding = result.findings[0]
    assert len(finding.title) == limits.MAX_TITLE_CHARS
    assert len(finding.description) <= limits.MAX_RICH_CHARS
    assert sum(len(b.html) for b in finding.poc) <= limits.MAX_POC_CHARS
    assert "truncated" in finding.flags and "text_truncated" in codes(result)


def test_file_name_and_title_are_not_used_for_the_company():
    result = read_report(
        report([detail_table("001", "T", severity="Low")], company="Real Client"),
        file_name="Alfred AI.docx",
    )
    assert result.header.company == "Real Client"


def test_the_reader_modules_import_without_lxml():
    code = (
        "import sys\n"
        "sys.modules['lxml'] = None\n"
        "import pwndoc_ai.docx_import.labels, pwndoc_ai.docx_import.html, pwndoc_ai.docx_import.images\n"
        "import pwndoc_ai.docx_import.captions, pwndoc_ai.docx_import.dedupe\n"
        "import pwndoc_ai.docx_import.reader\n"
        "print('ok')\n"
    )
    src = str(Path(__file__).resolve().parents[2] / "src")
    env = {**os.environ, "PYTHONPATH": src}
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env=env
    )
    assert out.stdout.strip() == "ok", out.stderr
