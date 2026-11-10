"""Audit-level fields: company vote, leftover names, dates, scope, contacts, severity table."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("lxml")

from reader_fixtures import cell, detail_table, p, report, row, table  # noqa: E402

from pwndoc_ai.docx_engine.xmlutil import parse_xml  # noqa: E402
from pwndoc_ai.docx_import import header_fields as hf  # noqa: E402
from pwndoc_ai.docx_import.model import ImportedFinding  # noqa: E402
from pwndoc_ai.docx_import.reader import read_report  # noqa: E402
from pwndoc_ai.docx_import.reader_xml import rows  # noqa: E402

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
BRAND = Path("/tmp/layout/brand.docx")
DEMO = Path("/tmp/layout/demo.docx")
ONE = [detail_table("001", "SQL Injection", severity="High")]


def grid(xml: str) -> list:
    return rows(parse_xml(f"<w:body {NS}>{xml}</w:body>".encode())[0])


def header_of(data: bytes):
    return read_report(data, file_name="r.docx")


# ---------------------------------------------------------------- company


def test_company_is_the_vote_of_four_sources_and_the_leftover_is_flagged():
    result = header_of(report(ONE, company="Brandarmy"))
    assert (result.header.company, result.header.company_votes) == ("Brandarmy", 4)
    assert result.header.leftover_names == ("Alfred AI",)
    assert "leftover_name" in {i.code for i in result.issues}
    assert "company_conflict" not in {i.code for i in result.issues}


def test_the_leftover_name_is_not_flagged_when_it_is_the_client():
    result = header_of(report(ONE, company="Alfred AI"))
    assert result.header.company == "Alfred AI" and result.header.leftover_names == ()


def test_body_prose_is_never_a_company_source():
    result = header_of(report(ONE, company="Acme", leftover=True))
    assert "Alfred AI" not in result.header.company_candidates


def test_vote_majority_and_tie_break():
    assert hf.vote({"header": "A", "application": "B", "cover": "b", "disclaimer": "C"}) == (
        "B",
        2,
        ("A", "B", "C"),
    )
    assert hf.vote({"cover": "X", "disclaimer": "Y"}) == ("X", 1, ("X", "Y"))
    assert hf.vote({}) == ("", 0, ())


def test_disagreeing_sources_raise_a_conflict():
    data = report(ONE, company="Acme", disclaimer_company="Other")
    result = header_of(data)
    assert result.header.company == "Acme" and "Other" in result.header.company_candidates
    assert "company_conflict" in {i.code for i in result.issues}


def test_the_fallback_copy_of_the_cover_box_is_ignored():
    from reader_fixtures import package, textbox

    body = textbox(
        p("Penetration Testing Report - Choice Co"),
        fallback=p("Penetration Testing Report - Fallback Co"),
    )
    data = package(body + ONE[0])
    result = header_of(data)
    assert result.header.company == "Choice Co"
    assert "Fallback Co" not in result.header_text


# ---------------------------------------------------------------- dates, scope, contacts


def test_table_dates_are_converted_to_iso():
    result = header_of(report(ONE, start="05.03.2026", end="6.3.2026"))
    assert (result.header.date_start, result.header.date_end) == ("2026-03-05", "2026-03-06")


def test_undefined_and_invalid_dates_are_blank():
    result = header_of(report(ONE, start="undefined", end="31.02.2026"))
    assert (result.header.date_start, result.header.date_end) == ("", "")


@pytest.mark.parametrize(
    ("text", "iso"),
    [("03/06/2026", ""), ("13/06/2026", "2026-06-13"), ("06/13/2026", "2026-06-13"),
     ("2026-06-13", "2026-06-13"), ("13.06.2026.", "2026-06-13"), ("undefined", "")],
)  # fmt: skip
def test_disclaimer_dates_only_when_unambiguous(text, iso):
    assert hf.disclaimer_date(text) == iso


def test_disclaimer_date_is_only_a_fallback_for_the_end_date():
    data = report(ONE, start="undefined", end="undefined", tested="13/06/2026")
    result = header_of(data)
    assert (result.header.date_start, result.header.date_end) == ("", "2026-06-13")


def test_scope_items_one_per_paragraph():
    result = header_of(report(ONE, scope=("https://a", "https://b/", "undefined", "https://a")))
    assert result.header.scope == ("https://a", "https://b/")


def test_contacts_from_labelled_rows():
    xml = table(
        row(cell(p("Contact")), cell(p("Jane Doe <jane.doe@acme.example>"))),
        row(cell(p("Email")), cell(p("ops@acme.example"))),
        row(cell(p("Contact")), cell(p("no address here"))),
    )
    contacts = hf.read_contacts(hf.FrontTables(other=(grid(xml),)))
    assert [(c.email, c.firstname, c.lastname) for c in contacts] == [
        ("jane.doe@acme.example", "Jane", "Doe"),
        ("ops@acme.example", "", ""),
    ]


def test_this_template_has_no_contacts():
    result = header_of(report(ONE))
    assert result.header.contacts == ()
    assert "contacts_absent" in {i.code for i in result.issues}


# ---------------------------------------------------------------- severity table


def test_severity_table_mismatch_is_reported():
    result = header_of(report(ONE, severity_counts={"High": "0", "Low": "0"}))
    assert result.header.severity_table == (("High", 0), ("Low", 0))
    assert "severity_table_mismatch" in {i.code for i in result.issues}


def test_matching_severity_table_is_quiet():
    result = header_of(report(ONE, severity_counts={"High": "1", "Low": "0"}))
    assert "severity_table_mismatch" not in {i.code for i in result.issues}


def test_severity_mismatch_counts_included_findings_only():
    findings = [ImportedFinding(key="f001", title="t", severity="High", include=False)]
    assert hf.severity_mismatch((("High", 0),), findings) is False


def test_classify_front_tables():
    assert hf.classify(grid(table(row(cell(p("Start Date")), cell(p("End Date")))))) == "dates"
    assert hf.classify(grid(table(row(cell(p("S. No")), cell(p("Scope URL")))))) == "scope"
    assert hf.classify(grid(table(row(cell(p("S. No")), cell(p("Name of the Tool")))))) == "other"


def test_header_text_holds_front_matter_but_no_finding_text():
    data = report([detail_table("001", "Secret finding title", description=[p("hidden body")])])
    text = header_of(data).header_text
    assert "Acme Corp" in text and "Start Date" in text
    assert "Secret finding title" not in text and "hidden body" not in text


# ---------------------------------------------------------------- real samples


@pytest.mark.skipif(not BRAND.exists(), reason="sample report not present")
def test_brandarmy_sample_header():
    header = read_report(BRAND.read_bytes(), file_name=BRAND.name).header
    assert (header.company, header.company_votes) == ("Brandarmy", 4)
    assert header.leftover_names == ("Alfred AI",)
    assert (header.date_start, header.date_end) == ("", "")
    assert header.scope == ("https://brandarmy.com", "https://wink.ing/")


@pytest.mark.skipif(not DEMO.exists(), reason="sample report not present")
def test_demo_sample_header():
    header = read_report(DEMO.read_bytes(), file_name=DEMO.name).header
    assert header.company == "Alfred AI" and header.leftover_names == ()
    assert (header.date_start, header.date_end) == ("2026-03-05", "2026-03-06")
    assert header.scope == ("http://192.168.1.1/24",)
