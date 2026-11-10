"""PwnDoc lookups: options, defaults from the newest audit, case-insensitive name checks."""

from __future__ import annotations

from test_writer_fakes import TEMPLATE_ID, FakePwnDoc

from pwndoc_ai.docx_import.lookups import (
    check_names,
    find_company,
    is_duplicate_audit,
    load_options,
)

T2 = "8" * 24


def rich_fake() -> FakePwnDoc:
    fake = FakePwnDoc()
    fake.languages = [
        {"language": "English", "locale": "en"},
        {"language": "Deutsch", "locale": "de"},
        {"language": "bad", "locale": "x/../y"},
        {"language": "dup", "locale": "en"},
        "junk",
    ]
    fake.audit_types = [
        {
            "name": "Web",
            "stage": "default",
            "templates": [{"template": TEMPLATE_ID, "locale": "en"}],
        },
        {"name": "Retest", "stage": "retest", "templates": []},
        {"name": "Network", "templates": [{"template": {"_id": T2}, "locale": "de"}]},
        {"name": "bad\nname", "templates": []},
    ]
    fake.templates = [
        {"_id": TEMPLATE_ID, "name": "Final_1", "ext": "docx"},
        {"_id": T2, "name": "German", "ext": "docx"},
        {"_id": "../evil", "name": "x"},
        {"_id": "9" * 24, "name": "sheet", "ext": "xlsx"},
    ]
    return fake


def test_options_skip_invalid_entries():
    fake = rich_fake()
    with fake.open_client() as client:
        options, defaults = load_options(client)
    assert [c.value for c in options.languages] == ["en", "de"]
    assert [(a.name, a.locales) for a in options.audit_types] == [
        ("Web", ("en",)),
        ("Network", ("de",)),
    ]
    assert {c.value for c in options.templates} == {TEMPLATE_ID, T2}
    assert (defaults.language, defaults.audit_type, defaults.template_id) == (
        "en",
        "Web",
        TEMPLATE_ID,
    )
    assert all(method == "GET" for method, _ in fake.calls)


def test_defaults_come_from_the_newest_audit():
    fake = rich_fake()
    old = fake.add_audit("old", None, "2025-01-01T00:00:00Z")
    new = fake.add_audit("new", None, "2026-05-01T00:00:00Z")
    fake.audits[old]["general"] = {"template": TEMPLATE_ID}
    fake.audits[new].update(language="de", auditType="Network", general={"template": T2})
    with fake.open_client() as client:
        options, defaults = load_options(client)
    assert (defaults.language, defaults.audit_type, defaults.template_id) == ("de", "Network", T2)
    assert options.templates[0].value == T2  # the default template is listed first


def test_unreadable_newest_audit_falls_back_to_the_type_template():
    fake = rich_fake()
    newest = fake.add_audit("new", None, "2026-05-01")
    fake.audits[newest].update(language="de", auditType="Network")
    fake.fail("GET", f"/api/audits/{newest}/general", 403)
    with fake.open_client() as client:
        _, defaults = load_options(client)
    assert defaults.template_id == T2  # Network's template for "de"


def test_no_audits_and_no_options():
    fake = FakePwnDoc(languages=[], audit_types=[], templates=[])
    with fake.open_client() as client:
        options, defaults = load_options(client)
    assert options.languages == () and defaults.template_id == ""


def test_company_match_is_case_insensitive():
    companies = [{"_id": "1" * 24, "name": "Other"}, {"_id": "2" * 24, "name": "BrandArmy "}]
    assert find_company(companies, "brandarmy") == "2" * 24
    assert find_company(companies, "Brand army") == ""
    assert find_company(companies, "") == ""
    assert find_company([{"_id": "../x", "name": "brandarmy"}], "Brandarmy") == ""


def test_duplicate_audit_needs_the_same_company():
    cid = "2" * 24
    audits = [
        {"_id": "3" * 24, "name": "Q1 WEB", "company": {"_id": cid, "name": "x"}},
        {"_id": "4" * 24, "name": "Q2", "company": None},
    ]
    assert is_duplicate_audit(audits, "q1 web", cid)
    assert not is_duplicate_audit(audits, "q1 web", "5" * 24)
    assert not is_duplicate_audit(audits, "q1 web", "")
    assert is_duplicate_audit(audits, "Q2", "")  # a new company: compare audits without one
    assert not is_duplicate_audit(audits, "", "")


def test_check_names_against_pwndoc():
    fake = FakePwnDoc()
    fake.companies = [{"_id": "2" * 24, "name": "Brandarmy"}]
    fake.add_audit("brandarmy", "2" * 24)
    with fake.open_client() as client:
        names = check_names(client, "BRANDARMY", "Brandarmy")
        fresh = check_names(client, "Newco", "brandarmy")
    assert names.company_id == "2" * 24 and names.duplicate_audit
    assert fresh.company_id == "" and not fresh.duplicate_audit
