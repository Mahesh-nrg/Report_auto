"""The import preview: defaults, build_plan and the strict merge of the UI's edits."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from plan_fakes import (
    COMPANY_ID,
    OTHER_TEMPLATE,
    TEMPLATE,
    build,
    codes,
    finding,
    header,
    image,
    report,
    text_tools,  # noqa: F401 - autouse fixture
)

from pwndoc_ai.docx_import import limits, plan
from pwndoc_ai.docx_import.errors import ImportConflict, ImportInputError
from pwndoc_ai.docx_import.model import (
    Contact,
    ImportPlan,
    NameCheck,
    PlanDefaults,
    PlanEdits,
    PocBlock,
    PwnDocOptions,
    check_references,
)

SAMPLES = (
    (Path("/tmp/refine_test/brandarmy.docx"), "Brandarmy", 10),
    (Path("/tmp/refine_test/clean.docx"), "Brandarmy", 10),
    (Path("/tmp/layout/demo.docx"), "Alfred AI", 1),
)


def edits(revision: int = 1, **body) -> PlanEdits:
    return PlanEdits.from_json({"revision": revision, **body})


# -- default_audit_name -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Brandarmy_VAPT.docx", "Brandarmy_VAPT"),
        ("report.DOCX", "report"),
        ("  Q1   web\tapp .docx", "Q1 web app"),
        ("C:\\Users\\x\\Acme Report.docx", "Acme Report"),
        ("../../etc/passwd.docx", "passwd"),
        ("a\x00b\x1fc.docx", "a b c"),
        ("bidi\u202eevil.docx", "bidi evil"),
        (".docx", "Imported report"),
        ("", "Imported report"),
        ("   ", "Imported report"),
    ],
)
def test_default_audit_name(name: str, expected: str) -> None:
    assert plan.default_audit_name(name) == expected


def test_default_audit_name_is_capped() -> None:
    assert len(plan.default_audit_name("x" * 900 + ".docx")) == limits.MAX_NAME_CHARS


# -- build_plan ---------------------------------------------------------------------------------


def test_build_plan_uses_file_name_and_never_the_title() -> None:
    built = build(report(header_text="Report title: Alfred AI Security Review"))
    assert built.revision == 1
    assert built.header.audit_name == "Brandarmy_VAPT_Report"
    assert built.header.company == "Brandarmy"
    assert built.header.language == "en"
    assert built.header.audit_type == "Web"
    assert built.header.template_id == TEMPLATE
    assert built.header.scope == ("https://brandarmy.com", "https://wink.ing/")
    assert (built.header.date_start, built.header.date_end) == ("2026-03-05", "2026-03-06")
    assert built.header.contact is None
    assert built.source_header.company == "Brandarmy"
    check_references(built)


def test_build_plan_round_trips_and_carries_names() -> None:
    built = build(names=NameCheck(COMPANY_ID, True), previous_import="f" * 12 + "4fff8" + "f" * 15)
    assert ImportPlan.from_json(built.to_json()) == built
    assert built.company_id == COMPANY_ID and built.duplicate_audit
    assert {"company_existing", "duplicate_audit", "previous_import"} <= set(codes(built.issues))


def test_build_plan_contact_is_the_first() -> None:
    first, second = Contact("a@example.com", "Ann"), Contact("b@example.com")
    built = build(report(header=header(contacts=(first, second))))
    assert built.header.contact == first
    assert "contacts_absent" not in codes(built.issues)


def test_defaults_outside_the_options_fall_back_to_the_first_choice() -> None:
    built = build(defaults=PlanDefaults("de", "Network", "f" * 24))
    assert built.header.language == "en"
    assert built.header.audit_type == "Web"
    assert built.header.template_id == TEMPLATE


def test_audit_type_prefers_one_with_a_template_for_the_language() -> None:
    built = build(defaults=PlanDefaults("en", "Mobile", TEMPLATE))
    assert built.header.audit_type == "Web"  # "Mobile" has no English template
    kept = build(defaults=PlanDefaults("fr", "Mobile", TEMPLATE))
    assert kept.header.audit_type == "Mobile"


def test_no_options_keep_the_defaults_and_flag_blanks() -> None:
    built = build(options=PwnDocOptions(), defaults=PlanDefaults())
    assert {"language_missing", "audit_type_missing", "template_missing"} <= set(
        codes(built.issues)
    )


def test_build_plan_refuses_a_report_breaking_the_model() -> None:
    bad = report(findings=(finding("f001", title="x" * (limits.MAX_TITLE_CHARS + 1)),))
    with pytest.raises(ImportInputError):
        build(bad)


# -- apply_edits --------------------------------------------------------------------------------


def test_stale_revision_is_refused() -> None:
    with pytest.raises(ImportConflict) as exc:
        plan.apply_edits(build(), edits(revision=5))
    assert exc.value.code == "stale"


def test_header_edit_bumps_revision_and_recomputes_issues() -> None:
    built = build()
    body = {"header": {"company": "  Brand   Army ", "date_end": "", "contact": None}}
    changed = plan.apply_edits(built, edits(**body))
    assert changed.revision == 2
    assert changed.header.company == "Brand Army"
    assert changed.header.date_end == ""
    assert changed.header.audit_name == built.header.audit_name
    assert "dates_missing" in codes(changed.issues)
    assert built.revision == 1 and built.header.date_end == "2026-03-06"  # not mutated


def test_header_choice_outside_the_options_is_invalid() -> None:
    with pytest.raises(ImportInputError) as exc:
        plan.apply_edits(build(), edits(header={"template_id": "f" * 24}))
    assert exc.value.field == "header.template_id"
    with pytest.raises(ImportInputError):
        plan.apply_edits(build(), edits(header={"language": "xx"}))


def test_header_template_change_within_options() -> None:
    changed = plan.apply_edits(build(), edits(header={"template_id": OTHER_TEMPLATE}))
    assert changed.header.template_id == OTHER_TEMPLATE


def test_company_change_forgets_the_old_match_unless_names_given() -> None:
    built = build(names=NameCheck(COMPANY_ID, True))
    renamed = plan.apply_edits(built, edits(header={"company": "Other Co"}))
    assert renamed.company_id == "" and not renamed.duplicate_audit
    assert "company_new" in codes(renamed.issues)
    checked = plan.apply_edits(
        built, edits(header={"company": "Other Co"}), names=NameCheck("9" * 24, False)
    )
    assert checked.company_id == "9" * 24
    case_only = plan.apply_edits(built, edits(header={"company": "BRANDARMY"}))
    assert case_only.company_id == COMPANY_ID


def test_unknown_finding_or_image_key() -> None:
    for body in (
        {"findings": [{"key": "f099", "title": "x"}]},
        {"images": [{"key": "i099", "caption": "x"}]},
        {"images": [{"key": "i001", "finding_key": "f099"}]},
        {"findings": [{"key": "f001", "poc": [{"kind": "image", "image_key": "i099"}]}]},
    ):
        with pytest.raises(ImportInputError) as exc:
            plan.apply_edits(build(), edits(**body))
        assert exc.value.code == "unknown_key"


def test_including_an_unusable_image_is_refused() -> None:
    rep = report(images=(image("i001", include=False, flags=("unsupported",), media_type=""),))
    with pytest.raises(ImportInputError) as exc:
        plan.apply_edits(build(rep), edits(images=[{"key": "i001", "include": True}]))
    assert exc.value.code == "image_not_usable"
    moved = plan.apply_edits(build(rep), edits(images=[{"key": "i001", "finding_key": "f002"}]))
    assert moved.images[0].finding_key == "f002"


def test_rich_fields_are_re_cleaned() -> None:
    hostile = (
        '<p onclick="x()">Hi <script>alert(1)</script><a href="javascript:x">'
        'link</a><img src=x onerror=y></p><iframe src="//evil"></iframe>'
    )
    body = {"key": "f001", "description": hostile, "observation": "<b>seen</b> &amp; noted"}
    changed = plan.apply_edits(build(), edits(findings=[body]))
    description = changed.findings[0].description
    for bad in ("<script", "onclick", "javascript", "<img", "<a", "iframe", "alert(1)"):
        assert bad not in description
    assert "Hi" in description and "link" in description
    observation = changed.findings[0].observation
    assert "<b>" not in observation and "seen" in observation and "&amp; noted" in observation


def test_injected_cleaner_is_used() -> None:
    calls: list[str] = []

    def cleaner(value: str) -> str:
        calls.append(value)
        return "<p>clean</p>"

    body = {"key": "f001", "remediation": "<p>raw</p>"}
    changed = plan.apply_edits(build(), edits(findings=[body]), clean=cleaner)
    assert calls == ["<p>raw</p>"]
    assert changed.findings[0].remediation == "<p>clean</p>"


def test_poc_html_cleaned_and_empty_blocks_dropped() -> None:
    poc = [
        {"kind": "html", "html": "<p>1. Open</p>"},
        {"kind": "html", "html": "<script>x</script>"},
        {"kind": "image", "image_key": "i001"},
    ]
    changed = plan.apply_edits(build(), edits(findings=[{"key": "f001", "poc": poc}]))
    assert changed.findings[0].poc == (
        PocBlock("html", html="<p>1. Open</p>"),
        PocBlock("image", image_key="i001"),
    )


def test_moving_an_image_removes_its_blocks_elsewhere() -> None:
    rep = report(
        findings=(
            finding(
                "f001", poc=(PocBlock("html", html="<p>a</p>"), PocBlock("image", image_key="i001"))
            ),
            finding("f002", title="Other"),
        )
    )
    built = build(rep)
    moved = plan.apply_edits(built, edits(images=[{"key": "i001", "finding_key": "f002"}]))
    assert all(b.kind == "html" for b in moved.findings[0].poc)
    assert moved.images[0].finding_key == "f002"
    detached = plan.apply_edits(built, edits(images=[{"key": "i001", "finding_key": ""}]))
    assert all(b.kind == "html" for f in detached.findings for b in f.poc)
    check_references(detached)


def test_a_foreign_or_repeated_image_block_is_dropped() -> None:
    poc = [
        {"kind": "image", "image_key": "i001"},
        {"kind": "html", "html": "<p>x</p>"},
        {"kind": "image", "image_key": "i001"},
    ]
    other = plan.apply_edits(build(), edits(findings=[{"key": "f002", "poc": poc}]))
    assert [b.kind for b in other.findings[1].poc] == ["html"]
    own = plan.apply_edits(build(), edits(findings=[{"key": "f001", "poc": poc}]))
    assert [b.kind for b in own.findings[0].poc] == ["image", "html"]


def test_plain_fields_are_one_line_and_deduplicated() -> None:
    body = {
        "key": "f001",
        "title": "  SQL   injection ",
        "urls": [" https://a ", "https://a", "https://b"],
        "references": ["CWE-89", " CWE-89"],
        "include": False,
    }
    changed = plan.apply_edits(build(), edits(findings=[body]))
    first = changed.findings[0]
    assert first.title == "SQL injection"
    assert first.urls == ("https://a", "https://b")
    assert first.references == ("CWE-89",)
    assert not first.include


def test_excluding_every_finding_is_a_blocker() -> None:
    body = [{"key": "f001", "include": False}, {"key": "f002", "include": False}]
    changed = plan.apply_edits(build(), edits(findings=body))
    assert "no_findings" in codes(changed.issues)


def test_an_edit_over_the_caps_fails_strict_validation() -> None:
    huge = "<p>" + "&" * (limits.MAX_RICH_CHARS // 4) + "</p>"  # escaping grows it 5x
    with pytest.raises(ImportInputError):
        plan.apply_edits(build(), edits(findings=[{"key": "f001", "description": huge}]))


def test_revision_cap() -> None:
    built = replace(build(), revision=limits.MAX_REVISION)
    with pytest.raises(ImportInputError):
        plan.apply_edits(built, edits(revision=limits.MAX_REVISION))


def test_result_passes_check_references_and_round_trip() -> None:
    body = {
        "header": {"scope": ["https://a", "https://a", " https://b "]},
        "images": [{"key": "i001", "caption": " New   caption ", "include": False}],
    }
    changed = plan.apply_edits(build(), edits(**body))
    assert changed.header.scope == ("https://a", "https://b")
    assert changed.images[0].caption == "New caption"
    assert ImportPlan.from_json(changed.to_json()) == changed


# -- real samples (read-only copies; skipped when absent) ---------------------------------------


def read_sample(path: Path):
    if not path.is_file():
        pytest.skip("sample report not present")
    pytest.importorskip("lxml")
    try:
        from pwndoc_ai.docx_import import reader
    except ImportError:
        pytest.skip("reader not built yet")
    return reader.read_report(path.read_bytes(), file_name=path.name)


@pytest.mark.parametrize(
    ("path", "company", "count"), SAMPLES, ids=lambda v: str(getattr(v, "name", v))
)
def test_plan_from_real_sample(path: Path, company: str, count: int) -> None:
    built = build(read_sample(path), file_name=path.name)
    assert built.header.audit_name == path.stem
    assert built.header.company == company
    assert len(built.findings) == count
    assert ImportPlan.from_json(built.to_json()) == built
    found = codes(built.issues)
    assert "leftover_name" in found  # Brandarmy: body leftover; demo: company is the leftover
    assert "company_new" in found and "contacts_absent" in found
