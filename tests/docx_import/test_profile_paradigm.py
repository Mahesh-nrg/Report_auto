"""The ``paradigm`` layout profile of the DOCX reader (ParadigmIT VAPT report layout).

Synthetic documents check detection and each hook; the generated import sample and the full
Word conversion (deliverable 1, 19 findings) check the whole read. File-based tests skip when
the generated documents are absent; the default profile is checked to stay unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("lxml")

from reader_fixtures import cell, header_line, p, package, picture, png, row, table  # noqa: E402

from pwndoc_ai.docx_import import ordering, profile, profile_paradigm  # noqa: E402
from pwndoc_ai.docx_import.findings import pair_figures, parse_blob, split_urls  # noqa: E402
from pwndoc_ai.docx_import.html import Para  # noqa: E402
from pwndoc_ai.docx_import.reader import read_report  # noqa: E402
from pwndoc_ai.docx_import.reader_xml import ImageRef  # noqa: E402

GENERATED = Path(__file__).resolve().parents[3] / "report_templates" / "generated"
SAMPLE = GENERATED / "ParadigmIT_VAPT_Import_Sample.docx"
FULL = GENERATED / "ParadigmIT_VAPT_Report.docx"
BRAND = Path("/tmp/layout/brand.docx")
DEMO = Path("/tmp/layout/demo.docx")
LABELS = (
    "S. No.", "Name of Vulnerability", "Vulnerable Location", "Vulnerable Path / URL",
    "Vulnerable Parameter", "CVE / CWE", "CVSS Score", "Severity", "Description",
)  # fmt: skip


def needs(path: Path):
    return pytest.mark.skipif(not path.is_file(), reason=f"{path.name} not present")


def _detail(values: list[str], steps: list[str], refs: str = "N/A") -> str:
    rows = [
        row(cell(p(label)), cell(p(value))) for label, value in zip(LABELS, values, strict=True)
    ]
    rows.append(
        row(cell(p("Proof of Concept")), cell(picture("rId9"), p("Representative example - x")))
    )
    rows.append(row(cell(p("Steps to Reproduce")), cell(*[p(f"{s}") for s in steps])))
    rows.append(row(cell(p("Recommendations")), cell(p("Fix it."))))
    rows.append(row(cell(p("References")), cell(p(refs))))
    return table(*rows)


def _cover() -> str:
    return table(
        row(
            cell(p("ASSESSMENT WINDOW"), p("31 AUG - 08 SEP 2026")),
            cell(p("PREPARED FOR"), p("Acme Corp")),
        )
    )


def _register() -> str:
    head = row(
        cell(p("S. No.")), cell(p("Vulnerable location, parameter, path")),
        cell(p("Name of the vulnerability")), cell(p("Reference (CWE-ID)")), cell(p("Severity")),
    )  # fmt: skip
    body = row(
        cell(p("001"), p("https://a.example.com/x"), p("Param: id"), p("Location: Login - form")),
        cell(p("Weak Thing")), cell(p("CWE-1")), cell(p("High")),
    )  # fmt: skip
    return table(head, body)


def _doc(cover: bool = True) -> bytes:
    values = ["001", "Weak Thing", "Login → form", "https://a.example.com/x?", "id", "CWE-1",
              "N/A", "High", "Desc."]  # fmt: skip
    body = (_cover() if cover else "") + _register() + _detail(values, ["Open", "Send"])
    hdr = header_line("PARADIGMIT CYBERSECURITY / VAPT" if cover else "Acme Corp")
    return package(body, media={"rId9": png()}, header=hdr)


# -- detection ---------------------------------------------------------------------------------
def test_two_markers_select_the_paradigm_profile_and_one_does_not():
    strong = "PARADIGMIT CYBERSECURITY / VAPT"
    assert profile.detect(["PREPARED FOR", "x", strong]) is profile.PARADIGM
    assert profile.detect(["6. Detailed technical findings", "ASSESSMENT WINDOW"]) is (
        profile.PARADIGM
    )
    assert profile.detect(["PREPARED FOR", "ASSESSMENT WINDOW"]) is profile.DEFAULT, (
        "two weak markers without the header or the findings heading do not switch"
    )
    assert profile.detect(["Steps to Reproduce", "PREPARED FOR"]) is profile.DEFAULT
    assert profile.detect([]) is profile.DEFAULT
    assert profile.active() is profile.DEFAULT, "outside a read the default is active"


def test_the_default_profile_keeps_the_original_behaviour():
    assert parse_blob("https://a/x\nParam: id\nLocation: L") == {"location": "L"}
    figure = pair_figures([ImageRef("rId1", 1), Para("Representative example - x")])
    assert not figure[0].captioned, "free captions are a profile feature"


def test_profile_hooks_when_active():
    with profile.use(profile.PARADIGM):
        blob = parse_blob("https://a/x\nParam: id\nLocation: L")
        assert blob == {"urls": ("https://a/x",), "parameter": "id", "location": "L"}
        assert split_urls("https://a/x?\ndata={1}\nhttps://b/y")[0] == (
            "https://a/x?data={1}",
            "https://b/y",
        )
        items = pair_figures([ImageRef("rId1", 1), Para("Representative example - x")])
        assert items[0].caption == "Representative example - x"
        lead = pair_figures([ImageRef("rId1", 1), Para("Steps to reproduce:")])
        assert not lead[0].captioned, "a line ending with a colon is not a caption"


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("31 AUG - 08 SEP 2026", "2026-08-31", "2026-09-08"),
        ("03 Mar - 14 Mar 2026", "2026-03-03", "2026-03-14"),
        ("28 Dec - 04 Jan 2027", "2026-12-28", "2027-01-04"),
        ("2026-05-01 to 2026-05-09", "2026-05-01", "2026-05-09"),
        ("31/08/2026 - 08/09/2026", "2026-08-31", "2026-09-08"),
        ("01/02/2026 \u2013 03/02/2026", "2026-02-01", "2026-02-03"),
        ("09/09/2026 - 08/09/2026", "", ""),
        ("TBD", "", ""),
    ],
)
def test_assessment_window(text, start, end):
    assert profile_paradigm.parse_window(text) == (start, end)


# -- a synthetic report --------------------------------------------------------------------------
def test_synthetic_report_reads_cover_dedupes_and_keeps_steps():
    report = read_report(_doc(), file_name="x.docx")
    assert report.header.company == "Acme Corp", "vendor header and 'the Client' never win"
    assert (report.header.date_start, report.header.date_end) == ("2026-08-31", "2026-09-08")
    (finding,) = report.findings
    assert finding.location == "Login → form", "the detail copy wins over the register"
    assert finding.urls == ("https://a.example.com/x?",)
    assert finding.cvss_score == "" and finding.references == ()
    html = "".join(b.html for b in finding.poc if b.kind == "html")
    assert "<strong>Steps to reproduce:</strong>" in html and "Open" in html
    assert report.images[0].caption == "Representative example - x"


def test_without_cover_markers_the_default_profile_reads_it():
    report = read_report(_doc(cover=False), file_name="x.docx")
    assert report.header.company_candidates[0] == "Acme Corp", "the header vote, as before"
    html = "".join(b.html for b in report.findings[0].poc if b.kind == "html")
    assert "Steps to reproduce" not in html


# -- the generated import sample ---------------------------------------------------------------
@needs(SAMPLE)
def test_sample_header_fields():
    report = read_report(SAMPLE.read_bytes(), file_name=SAMPLE.name)
    header = report.header
    assert header.company == "Northwind Traders" and header.company_candidates == (
        "Northwind Traders",
    )
    assert header.scope == ("portal.example.org",)
    assert (header.date_start, header.date_end) == ("2026-03-03", "2026-03-14")
    assert header.test_type == "Grey-box penetration test"
    expected = (("Critical", 1), ("High", 1), ("Medium", 2), ("Low", 1), ("Informational", 1))
    assert header.severity_table == expected
    codes = {i.code for i in report.issues}
    assert "severity_table_mismatch" not in codes and "company_conflict" not in codes


@needs(FULL)
def test_full_word_conversion_reads_19_findings():
    report = read_report(FULL.read_bytes(), file_name=FULL.name)
    assert len(report.findings) == 19 and len(report.images) == 19
    assert report.stats.summary_rows == 19 and report.stats.detail_tables == 19
    assert report.header.company == "Example Client"
    assert (report.header.date_start, report.header.date_end) == ("2026-08-31", "2026-09-08")
    assert all(i.caption == "Representative example — example.com" for i in report.images)
    assert all(i.flags == () for i in report.images), "no unmatched/uncaptioned picture"
    assert [f.severity for f in report.findings].count("Medium") == 7
    # the generated report lists its findings in FINDING_ORDER.md order: a re-import is stable
    assert [f.key for f in ordering.sort_findings(report.findings)] == [
        f.key for f in report.findings
    ]
    assert [f.source_id for f in report.findings] == [f"{n:03d}" for n in range(1, 20)]
    first = next(f for f in report.findings if f.location == "Password reset → OTP")
    assert first.urls == (
        'https://app.example.com/api/v1/auth/otp/request?data={"email":"victim@example.com",'
        '"client_id":0,"device_id":0}',
    )
    codes = {i.code for i in report.issues}
    assert codes <= {"duplicates_merged", "contacts_absent"}


@needs(BRAND)
@pytest.mark.parametrize("path", [BRAND, DEMO])
def test_existing_samples_stay_on_the_default_profile(path):
    if not path.is_file():
        pytest.skip("sample not present")
    report = read_report(path.read_bytes(), file_name=path.name)
    assert report.header.company in ("Brandarmy", "Alfred AI")
    for finding in report.findings:
        html = "".join(b.html for b in finding.poc if b.kind == "html")
        assert "<strong>Steps to reproduce:</strong>" not in html


# -- review regressions (IMP-1 / IMP-2 / IMP-3) ---------------------------------------------------
RENDERED = GENERATED / "rendered_samples" / "s19.docx"


def test_prose_with_marker_words_keeps_the_default_profile():
    prose = [
        "This report was prepared for the Brandarmy security team.",
        "Testing took place within the agreed assessment window.",
        "Prepared for: Acme Holdings",
        "Assessment window: 1-5 May 2026",
    ]
    assert profile.detect(prose) is profile.DEFAULT


def test_pwndoc_label_row_over_evidence_row_keeps_the_picture():
    rows = [row(cell(p(label)), cell(p(value))) for label, value in zip(
        LABELS, ["001", "Weak Thing", "L", "https://a/x", "id", "CWE-1", "5.0", "High", "D."],
        strict=True,
    )]  # fmt: skip
    rows.append(row(cell(p("Proof of Concept"))))
    rows.append(row(cell(picture("rId9"), p("Representative example - x"))))
    rows.append(row(cell(p("Recommendations")), cell(p("Fix it."))))
    hdr = header_line("PARADIGMIT CYBERSECURITY / VAPT")
    body = _cover() + table(*rows)
    report = read_report(package(body, media={"rId9": png()}, header=hdr), file_name="x.docx")
    assert len(report.findings) == 1
    assert [i.caption for i in report.images] == ["Representative example - x"]
    assert report.findings[0].remediation


@needs(RENDERED)
def test_pwndoc_render_round_trip_keeps_images_captions_and_dates():
    report = read_report(RENDERED.read_bytes(), file_name="s19.docx")
    assert len(report.findings) == 19
    assert len(report.images) == 19
    assert {i.caption for i in report.images} == {"Representative example - example.com"}
    assert (report.header.date_start, report.header.date_end) == ("2026-08-31", "2026-09-08")
    assert report.header.company == "Example Client"


@needs(BRAND)
def test_brand_with_injected_marker_prose_stays_default(tmp_path):
    from docx import Document

    doc = Document(str(BRAND))
    doc.add_paragraph(
        "This report was prepared for the Brandarmy security team. Testing took place "
        "within the agreed assessment window."
    )
    doc.add_paragraph("Prepared for: Acme Holdings")
    doc.add_paragraph("Assessment window")
    out = tmp_path / "brand_mut.docx"
    doc.save(str(out))
    report = read_report(out.read_bytes(), file_name="b.docx")
    assert report.header.company == "Brandarmy"
    assert "severity_table_mismatch" in {i.code for i in report.issues}
