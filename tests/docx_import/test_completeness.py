"""The completeness panel: every code, its level and fields, order and sticky codes."""

from __future__ import annotations

from dataclasses import replace

import pytest
from plan_fakes import (
    COMPANY_ID,
    build,
    codes,
    finding,
    header,
    image,
    report,
    text_tools,  # noqa: F401 - autouse fixture
)

from pwndoc_ai.docx_import import completeness, limits
from pwndoc_ai.docx_import.model import (
    AuditHeader,
    CompletenessIssue,
    Contact,
    NameCheck,
    SourceStats,
)


def issues_of(plan) -> dict[str, CompletenessIssue]:
    return {issue.code: issue for issue in completeness.assess(plan)}


def with_header(**values):
    built = build()
    return replace(built, header=replace(built.header, **values))


def test_complete_plan_has_only_notes() -> None:
    built = build(names=NameCheck(COMPANY_ID, False))
    built = replace(built, header=replace(built.header, contact=Contact("a@example.com")))
    levels = {issue.code: issue.level for issue in completeness.assess(built)}
    assert levels == {"company_existing": "info", "duplicates_merged": "info"}
    assert not completeness.blockers(built.issues) and not completeness.warnings(built.issues)


@pytest.mark.parametrize(
    ("values", "code", "fields"),
    [
        ({"company": " "}, "company_missing", ("company",)),
        ({"company": "undefined"}, "company_missing", ("company",)),
        ({"audit_name": ""}, "audit_name_missing", ("audit_name",)),
        ({"language": ""}, "language_missing", ("language",)),
        ({"language": "de"}, "language_missing", ("language",)),
        ({"audit_type": ""}, "audit_type_missing", ("audit_type",)),
        ({"template_id": "f" * 24}, "template_missing", ("template_id",)),
        ({"date_start": "2026-02-30"}, "date_invalid", ("date_start",)),
        ({"date_start": "2026-03-09"}, "date_order", ("date_start", "date_end")),
    ],
)
def test_header_blockers(values: dict, code: str, fields: tuple) -> None:
    found = issues_of(with_header(**values))
    assert found[code].level == "blocker"
    assert found[code].fields == fields
    assert completeness.blockers(found.values())


def test_header_warnings_and_notes() -> None:
    found = issues_of(with_header(date_start="", date_end="", scope=()))
    assert found["dates_missing"].fields == ("date_start", "date_end")
    assert found["dates_missing"].level == "warning"
    assert found["scope_missing"].level == "warning"
    assert found["contacts_absent"].level == "info"
    assert found["company_new"].level == "info"
    assert "date_order" not in found


def test_company_existing_duplicate_and_previous_import() -> None:
    built = replace(
        build(),
        company_id=COMPANY_ID,
        duplicate_audit=True,
        previous_import="a" * 12 + "4aaa8" + "a" * 15,
    )
    found = issues_of(built)
    assert {"company_existing", "duplicate_audit", "previous_import"} <= set(found)
    assert "company_new" not in found
    assert found["duplicate_audit"].level == "warning"


def test_leftover_and_conflict_from_the_source_header() -> None:
    rep = report(
        header=header(
            company_candidates=("Brandarmy", "Brand Army Ltd"), leftover_names=("Alfred AI",)
        )
    )
    found = issues_of(build(rep))
    assert found["leftover_name"].fields == ("company",)
    assert found["company_conflict"].level == "warning"


def test_a_company_equal_to_a_template_leftover_is_flagged() -> None:
    found = issues_of(build(report(header=header(company="ALFRED  AI", company_candidates=()))))
    assert "leftover_name" in found
    same = report(header=header(leftover_names=("Brandarmy",)))
    assert "leftover_name" not in issues_of(build(same))  # the body names the company itself


def test_leftover_names_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    from pwndoc_ai.docx_import import labels

    monkeypatch.setattr(labels, "TEMPLATE_LEFTOVER_NAMES", frozenset({"brandarmy"}))
    assert "leftover_name" in issues_of(build())


def test_source_stats_codes() -> None:
    stats = SourceStats(merged=1, figures_deduped=3, source_ids=("001", "003"), id_gaps=("002",))
    found = issues_of(build(report(stats=stats)))
    assert found["id_gaps"].level == "warning"
    assert found["duplicates_merged"].level == "info"
    assert found["figures_deduped"].level == "info"


def test_severity_table_mismatch() -> None:
    printed = (("High", 0), ("Medium", 0), ("Low", 0), ("Informational", 0))
    found = issues_of(build(report(header=header(severity_table=printed))))
    assert "severity_table_mismatch" in found
    matching = (("High", 2), ("Medium", 0))
    assert "severity_table_mismatch" not in issues_of(
        build(report(header=header(severity_table=matching)))
    )


def test_finding_codes_and_fields() -> None:
    bare = finding(
        "f001",
        title="N/A",
        severity="",
        cvss_vector="",
        description="",
        poc=(),
        remediation="<p> </p>",
        flags=("summary_only", "truncated"),
        llm_fields=("severity",),
    )
    found = issues_of(build(report(findings=(bare,), images=())))
    assert found["title_missing"].level == "blocker"
    assert found["severity_missing"].fields == ("severity",)
    assert found["cvss_missing"].fields == ("cvss_vector",)
    assert found["fields_blank"].fields == ("description", "poc", "remediation")
    assert found["fields_blank"].finding_key == "f001"
    assert {"summary_only", "text_truncated", "llm_fields"} <= set(found)
    assert found["llm_fields"].fields == ("severity",)


def test_an_attached_screenshot_fills_the_poc() -> None:
    lone = finding("f001", poc=())
    found = issues_of(build(report(findings=(lone,), images=(image("i001", "f001"),))))
    assert "fields_blank" not in found
    none = issues_of(build(report(findings=(lone,), images=())))
    assert none["fields_blank"].fields == ("poc",)


def test_excluded_findings_are_not_assessed() -> None:
    off = finding("f002", title="", severity="", include=False)
    found = issues_of(build(report(findings=(finding("f001"), off))))
    assert "title_missing" not in found and "severity_missing" not in found
    nothing = issues_of(build(report(findings=(replace(off, title="x"),), images=())))
    assert nothing["no_findings"].level == "blocker"


def test_image_codes() -> None:
    images = (
        image("i001", "f001", flags=("unmatched",)),
        image("i002", "f001", flags=("placeholder",), include=False, sha256="b" * 64),
        image(
            "i003", "f002", flags=("unsupported",), media_type="", include=False, sha256="c" * 64
        ),
        image("i004", "f002", flags=("too_large",), include=False, sha256="e" * 64),
        image("i005", "f002", caption="", sha256="f" * 64),
    )
    found = completeness.assess(build(report(images=images)))
    by_image = [(i.code, i.image_key) for i in found if i.image_key]
    assert by_image == [
        ("image_unmatched", "i001"),
        ("image_placeholder", "i002"),
        ("image_unsupported", "i003"),
        ("image_too_large", "i004"),
        ("image_uncaptioned", "i005"),
    ]


def test_order_is_header_then_findings_then_images() -> None:
    rep = report(
        findings=(finding("f001", severity=""), finding("f002", cvss_vector="")),
        images=(image("i001", "f002", caption=""),),
    )
    found = completeness.assess(replace(build(rep), header=AuditHeader()))
    kinds = ["image" if i.image_key else "finding" if i.finding_key else "header" for i in found]
    assert kinds == sorted(kinds, key=["header", "finding", "image"].index)
    finding_keys = [i.finding_key for i in found if i.finding_key and not i.image_key]
    assert finding_keys == sorted(finding_keys)
    assert completeness.assess(build(rep)) == completeness.assess(build(rep))


def test_sticky_and_carried_codes_survive_reassessment() -> None:
    carried = (
        CompletenessIssue("llm_failed"),
        CompletenessIssue("llm_unavailable"),
        CompletenessIssue("llm_fields", fields=("company",)),
        CompletenessIssue("text_truncated"),
        CompletenessIssue("leftover_name"),  # re-derived, not carried
        CompletenessIssue("llm_failed"),  # duplicate collapses
    )
    found = codes(completeness.assess(build(report(issues=carried))))
    assert found.count("llm_failed") == 1
    assert {"llm_unavailable", "llm_fields", "text_truncated"} <= set(found)
    assert "leftover_name" not in found


def test_issue_count_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "MAX_ISSUES", 3)
    assert len(completeness.assess(with_header(company="", audit_name="", language=""))) == 3


def test_blockers_and_warnings_filters() -> None:
    issues = (
        CompletenessIssue("company_missing"),
        CompletenessIssue("dates_missing"),
        CompletenessIssue("company_new"),
    )
    assert codes(completeness.blockers(issues)) == ["company_missing"]
    assert codes(completeness.warnings(issues)) == ["dates_missing"]


def test_messages_carry_no_document_text() -> None:
    rep = report(findings=(finding("f001", title="", description="<p>Secret ACME text</p>"),))
    for issue in completeness.assess(build(rep)):
        assert "ACME" not in issue.message and "Brandarmy" not in issue.message


# -- closer review (COR-6, COR-9) ------------------------------------------------------------------
def test_an_unparseable_vector_counts_as_missing() -> None:
    bad = finding("f001", cvss_vector="CVSS:3.1/AV:Z/AC:L")
    found = issues_of(build(report(findings=(bad,))))
    assert found["cvss_missing"].finding_key == "f001"


def test_copies_that_disagree_are_flagged_for_review() -> None:
    flagged = finding("f001", flags=("merged_duplicate", "copies_disagree"))
    issue = issues_of(build(report(findings=(flagged,))))["copies_disagree"]
    assert issue.level == "warning" and issue.finding_key == "f001"
    assert issue.fields == ("severity", "cwe", "cvss_vector")
