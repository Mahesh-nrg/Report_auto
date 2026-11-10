"""Text normalisation and the label dictionary of the DOCX reader."""

from __future__ import annotations

import pytest

from pwndoc_ai.docx_import import labels


def test_norm_maps_invisible_and_typographic_characters():
    raw = "\u00a0Full\u00adAccount\u200b  Take\u2013over \u201cemail\u201d\u2019s\n"
    assert labels.norm(raw) == 'FullAccount Take-over "email"\'s'


def test_norm_applies_nfkc():
    assert labels.norm("\uff21\uff43\uff4d\uff45 \ufb01le") == "Acme file"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("WordPress XMLRPC User Enumeration", "WordPress XML-RPC User Enumeration"),
        ("SQL  Injection", "sql injection"),
        ("Cross\u2011Site Scripting (XSS)", "cross site scripting xss"),
    ],
)
def test_fold_title_makes_spelling_variants_equal(left, right):
    assert labels.fold_title(left) == labels.fold_title(right)


def test_fold_title_keeps_different_titles_apart():
    assert labels.fold_title("SQL Injection") != labels.fold_title("SSRF")


def test_name_key_is_case_insensitive():
    assert labels.name_key("  ACME\u00a0Corp ") == labels.name_key("acme corp")


@pytest.mark.parametrize(
    "text",
    ["", "  ", "undefined", "N/A", "n / a", "NA", "-", "--", "\u2014", "null", "None", "TBD", ":"],
)
def test_blank_placeholders(text):
    assert labels.is_blank(text)


@pytest.mark.parametrize("text", ["0", "Login page", "N/A page", "undefined behaviour"])
def test_real_values_are_not_blank(text):
    assert not labels.is_blank(text)


@pytest.mark.parametrize(
    ("label", "field"),
    [
        ("Name of Vulnerability: ", "title"),
        ("Vulnerable Path/ Port/ URL ", "urls"),
        ("Vulnerable Parameter ", "parameter"),
        ("Vulnerable Location", "location"),
        ("\t\t\tVulnerable Point / Error Location", "group"),
        ("CVE/CWE  ", "cwe"),
        ("CVSS Score", "cvss"),
        ("Severity", "severity"),
        ("Risk Rating", "severity"),
        ("Description ", "description"),
        ("Proof of concept:", "poc"),
        ("Recommendations ", "remediation"),
        ("Remediation", "remediation"),
        ("References  ", "references"),
        ("S. No.", "id"),
        ("Something else", ""),
    ],
)
def test_field_labels_and_synonyms(label, field):
    assert labels.field_of(label) == field


@pytest.mark.parametrize(
    ("text", "severity"),
    [("High", "High"), (" CRITICAL ", "Critical"), ("Moderate", "Medium"), ("Info", "Informational"),
     ("Low risk", "Low"), ("Severe", ""), ("", "")],
)  # fmt: skip
def test_severity_words(text, severity):
    assert labels.severity_of_text(text) == severity


def test_first_name_segment_splits_on_wide_gaps():
    assert (
        labels.first_name_segment("Brandarmy" + " " * 30 + "ParadigmIT Cybersecurity")
        == "Brandarmy"
    )
    assert labels.first_name_segment("\tAcme\tVendor") == "Acme"
    assert labels.first_name_segment("   ") == ""


def test_clean_line_collapses_breaks_and_drops_controls_but_keeps_quotes():
    assert labels.clean_line("a\tb\nc\x00d \u201cq\u201d") == "a b c d \u201cq\u201d"


def test_cap_reports_cutting():
    assert labels.cap("abcdef", 3) == ("abc", True)
    assert labels.cap("abc", 3) == ("abc", False)
