"""Strict validation of the importer's data model: round trips and hostile JSON."""

from __future__ import annotations

import copy

import pytest

from pwndoc_ai.docx_import import errors, limits, model
from pwndoc_ai.docx_import.errors import ImportInputError
from pwndoc_ai.docx_import.model import (
    AnalyseRequest,
    AuditHeader,
    CompletenessIssue,
    Contact,
    CreatedObject,
    CreateRequest,
    HeaderFields,
    ImportedFinding,
    ImportedImage,
    ImportPlan,
    ImportResult,
    ImportStatus,
    Journal,
    PlanEdits,
    PocBlock,
    PwnDocOptions,
    SourceStats,
    UndoItem,
    UndoResult,
)
from pwndoc_ai.docx_import.model import (
    Choice as Option,
)

IMPORT_ID = "0123456789ab4def8123456789abcdef"
FILE_ID = "fedcba9876544321a123456789abcdef"
AUDIT = "a" * 24
COMPANY = "b" * 24
TEMPLATE = "c" * 24
SHA = "d" * 64
STAMP = "2026-10-23T10:00:00Z"


def sample_finding() -> ImportedFinding:
    return ImportedFinding(
        key="f001",
        title="Stored XSS",
        source_id="003",
        severity="High",
        cvss_score="8.8",
        cvss_vector="CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H",
        cwe="CWE-79",
        urls=("https://a.example/x", "https://a.example/y"),
        parameter="q",
        location="Search",
        description="<p>Text &amp; more</p>",
        poc=(PocBlock("html", html="<p>Step</p>"), PocBlock("image", image_key="i001")),
        remediation="<ul><li><p>Encode</p></li></ul>",
        references=("https://owasp.org",),
        origin="merged",
        llm_fields=("observation",),
        flags=("description_split",),
    )


def sample_image() -> ImportedImage:
    return ImportedImage(
        key="i001",
        sha256=SHA,
        media_type="image/jpeg",
        size=36340,
        width=942,
        height=518,
        caption="Login page",
        finding_key="f001",
        source_finding_key="f001",
        order=3,
    )


def sample_plan() -> ImportPlan:
    return ImportPlan(
        import_id=IMPORT_ID,
        revision=2,
        file_name="Brandarmy report.docx",
        file_sha256=SHA,
        header=AuditHeader(
            company="Brandarmy",
            audit_name="Brandarmy report",
            language="en",
            audit_type="Alfred",
            template_id=TEMPLATE,
            date_start="2026-03-05",
            date_end="2026-03-06",
            scope=("https://brandarmy.com",),
            contact=Contact(email="a@b.example", firstname="Ann"),
        ),
        source_header=HeaderFields(
            company="Brandarmy",
            company_votes=4,
            company_candidates=("Brandarmy",),
            scope=("https://brandarmy.com",),
            leftover_names=("Alfred AI",),
            severity_table=(("High", 0),),
        ),
        findings=(sample_finding(),),
        images=(sample_image(),),
        issues=(
            CompletenessIssue("id_gaps"),
            CompletenessIssue("fields_blank", finding_key="f001", fields=("cwe", "references")),
            CompletenessIssue("image_uncaptioned", image_key="i001"),
        ),
        stats=SourceStats(summary_rows=1, detail_tables=1, merged=1, source_ids=("003",)),
        options=PwnDocOptions(
            languages=(Option("en", "English"),),
            templates=(Option(TEMPLATE, "Final_1"),),
        ),
        company_id=COMPANY,
    )


def plan_json() -> dict:
    return sample_plan().to_json()


def rejected(parse, value, field: str | None = None) -> ImportInputError:
    with pytest.raises(ImportInputError) as caught:
        parse(value)
    if field is not None:
        assert caught.value.field == field
    return caught.value


def test_a_full_plan_survives_a_json_round_trip_unchanged():
    plan = sample_plan()
    assert ImportPlan.from_json(copy.deepcopy(plan.to_json())) == plan


def test_issue_json_carries_the_level_and_the_fixed_message_of_its_code():
    data = CompletenessIssue("severity_missing", finding_key="f001").to_json()
    assert data["level"] == "warning"
    assert data["message"] == limits.ISSUES["severity_missing"][1]


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda d: d.update(extra=1), "plan"),
        (lambda d: d.pop("findings"), "plan"),
        (lambda d: d.update(revision=True), "plan.revision"),
        (lambda d: d.update(revision=-1), "plan.revision"),
        (lambda d: d.update(import_id="../etc"), "plan.import_id"),
        (lambda d: d.update(company_id="not-an-object-id"), "plan.company_id"),
        (lambda d: d["header"].update(date_start="2026-02-30"), "plan.header.date_start"),
        (lambda d: d["header"].update(language="en/../x"), "plan.header.language"),
        (lambda d: d["header"].update(company="A\x00B"), "plan.header.company"),
        (lambda d: d["header"].update(scope=["ok", ""]), "plan.header.scope"),
        (lambda d: d["findings"][0].update(title="T" * 301), "plan.findings[0].title"),
        (lambda d: d["findings"][0].update(title="line\nbreak"), "plan.findings[0].title"),
        (lambda d: d["findings"][0].update(severity="high"), "plan.findings[0].severity"),
        (lambda d: d["findings"][0].update(cvss_score="11"), "plan.findings[0].cvss_score"),
        (lambda d: d["findings"][0].update(cvss_vector="AV:N"), "plan.findings[0].cvss_vector"),
        (lambda d: d["findings"][0].update(key="x1"), "plan.findings[0].key"),
        (lambda d: d["findings"][0].update(llm_fields=["poc"]), "plan.findings[0].llm_fields[0]"),
        (lambda d: d["findings"][0].update(flags=["truncated", "truncated"]), None),
        (lambda d: d["findings"][0].update(include=1), "plan.findings[0].include"),
        (lambda d: d["findings"][0]["poc"].append({"kind": "raw", "html": "<x>"}), None),
        (lambda d: d["findings"][0]["poc"].append({"kind": "html", "html": "  "}), None),
        (lambda d: d["images"][0].update(media_type="image/svg+xml"), None),
        (lambda d: d["images"][0].update(sha256="z" * 64), None),
        (lambda d: d["issues"][0].update(level="info"), "plan.issues[0]"),
        (lambda d: d["issues"][0].update(message="<b>hi</b>"), "plan.issues[0]"),
        (lambda d: d["issues"][0].update(code="made_up"), "plan.issues[0].code"),
    ],
)
def test_a_tampered_plan_is_refused_with_the_path_of_the_bad_value(mutate, field):
    data = plan_json()
    mutate(data)
    rejected(ImportPlan.from_json, data, field)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["images"][0].update(finding_key="f999"),
        lambda d: d["findings"][0]["poc"].append({"kind": "image", "image_key": "i999"}),
        lambda d: d["issues"][1].update(finding_key="f999"),
        lambda d: d["findings"].append(copy.deepcopy(d["findings"][0])),
        lambda d: d["images"].append(copy.deepcopy(d["images"][0])),
    ],
)
def test_a_plan_whose_keys_point_nowhere_or_repeat_is_refused(mutate):
    data = plan_json()
    mutate(data)
    rejected(ImportPlan.from_json, data)


def test_an_unusable_image_cannot_be_included():
    data = plan_json()["images"][0]
    data.update(media_type="", flags=["unsupported"], include=True)
    rejected(ImportedImage.from_json, data, "image.include")
    data["include"] = False
    image = ImportedImage.from_json(data)
    assert not image.usable and not image.include


def test_too_many_findings_are_refused(monkeypatch):
    monkeypatch.setattr(model, "MAX_FINDINGS", 1)
    data = plan_json()
    second = copy.deepcopy(data["findings"][0])
    second["key"] = "f002"
    data["findings"].append(second)
    rejected(ImportPlan.from_json, data, "plan.findings")


def test_poc_html_over_the_total_cap_is_refused(monkeypatch):
    monkeypatch.setattr(model, "MAX_POC_CHARS", 10)
    data = plan_json()
    rejected(ImportPlan.from_json, data, "plan.findings[0].poc")


def test_edits_carry_only_the_fields_the_request_named():
    edits = PlanEdits.from_json(
        {
            "revision": 2,
            "header": {"audit_name": "Q3 web test", "contact": None},
            "findings": [{"key": "f001", "severity": "", "include": False}],
            "images": [{"key": "i001", "finding_key": ""}],
        }
    )
    assert edits.header is not None
    assert edits.header.present == {"audit_name", "contact"}
    assert edits.header.get("contact") is None
    assert edits.findings[0].present == {"severity", "include"}
    assert edits.images[0].get("finding_key") == ""


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"revision": 1, "header": {"company_id": COMPANY}},
        {"revision": 1, "findings": [{"key": "f001", "origin": "detail"}]},
        {"revision": 1, "findings": [{"severity": "High"}]},
        {"revision": 1, "findings": [{"key": "f001"}, {"key": "f001"}]},
        {"revision": 1, "images": [{"key": "i001", "sha256": SHA}]},
        {"revision": 1, "images": [{"key": "i001", "include": "yes"}]},
        {"revision": "1"},
        [],
        "x",
    ],
)
def test_bad_edit_bodies_are_refused(body):
    rejected(PlanEdits.from_json, body)


def test_analyse_and_create_requests_default_to_the_safe_choice():
    assert AnalyseRequest.from_json({"file_id": FILE_ID}).use_llm is True
    create = CreateRequest.from_json({"revision": 3})
    assert (create.accept_blanks, create.confirm_duplicate) == (False, False)
    rejected(AnalyseRequest.from_json, {"file_id": "../../x"})
    rejected(CreateRequest.from_json, {"revision": 3, "force": True})


def test_journal_round_trips_and_ties_images_and_findings_to_an_audit():
    journal = Journal(
        import_id=IMPORT_ID,
        state="complete",
        updated=STAMP,
        objects=(
            CreatedObject("company", COMPANY, STAMP),
            CreatedObject("audit", AUDIT, STAMP),
            CreatedObject("image", "e" * 24, STAMP, audit_id=AUDIT),
        ),
    )
    assert Journal.from_json(journal.to_json()) == journal
    bad = journal.to_json()
    bad["objects"][2]["audit_id"] = ""
    rejected(Journal.from_json, bad)
    bad = journal.to_json()
    bad["objects"][0]["audit_id"] = AUDIT
    rejected(Journal.from_json, bad)
    bad = journal.to_json()
    bad["objects"].append(bad["objects"][0])
    rejected(Journal.from_json, bad)
    bad = journal.to_json()
    bad["version"] = 2
    rejected(Journal.from_json, bad, "journal.version")


def test_status_with_result_and_undo_round_trips():
    status = ImportStatus(
        id=IMPORT_ID,
        file_id=FILE_ID,
        file_name="r.docx",
        state="undone",
        phase="undo",
        total=3,
        done=3,
        started=STAMP,
        ended=STAMP,
        revision=4,
        has_preview=True,
        result=ImportResult(audit_id=AUDIT, company_id=COMPANY, finding_ids=("f" * 24,)),
        undo=UndoResult((UndoItem("audit", AUDIT, "deleted"), UndoItem("image", "e" * 24, "kept"))),
    )
    assert ImportStatus.from_json(status.to_json()) == status
    assert status.undo is not None and status.undo.count("kept", "image") == 1
    bad = status.to_json()
    bad["state"] = "hacked"
    rejected(ImportStatus.from_json, bad, "status.state")


def test_every_issue_code_has_a_known_level_and_a_short_fixed_sentence():
    for level, message in limits.ISSUES.values():
        assert level in limits.ISSUE_LEVELS
        assert 0 < len(message) <= 120 and "<" not in message


def test_validation_errors_never_echo_the_rejected_value():
    secret = "ACME-SECRET-CLIENT"
    data = plan_json()
    data["findings"][0]["title"] = secret + "\x07"
    error = rejected(ImportPlan.from_json, data)
    assert secret not in str(error)
    assert error.status == 400


def test_error_messages_are_chosen_from_fixed_tables():
    assert str(errors.ImportConflict("stale")).startswith("the preview changed")
    assert str(errors.ImportConflict("no such code")) == errors.ImportConflict.MESSAGES["busy_user"]
    assert errors.ImportNotFound().status == 404
    assert errors.ImportLimitError("quota").status == 413
    assert str(ImportInputError(field="findings[2].title")) == "invalid value: findings[2].title"
    assert str(ImportInputError(field="<script>")) == "the request is not valid"
    assert str(ImportInputError("blanks")).startswith("some fields are blank")


# -- closer review (COR-9): an edited CVSS vector must parse -------------------------------------
@pytest.mark.parametrize("vector", ["CVSS:3.1/AV:Z/AC:L", "CVSS:3.1/AV:N"])
def test_an_edited_vector_that_does_not_parse_is_rejected(vector):
    body = {"revision": 1, "findings": [{"key": "f001", "cvss_vector": vector}]}
    with pytest.raises(ImportInputError) as caught:
        PlanEdits.from_json(body)
    assert caught.value.field.endswith("findings[0].cvss_vector")


def test_an_edited_vector_is_stored_canonical_and_blank_clears_it():
    vector = "CVSS:3.1/AC:L/AV:N/PR:L/UI:N/S:U/C:H/I:H/A:H"
    body = {"revision": 1, "findings": [{"key": "f001", "cvss_vector": vector}]}
    edit = PlanEdits.from_json(body).findings[0]
    assert edit.get("cvss_vector") == "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H"
    cleared = {"revision": 1, "findings": [{"key": "f001", "cvss_vector": ""}]}
    assert PlanEdits.from_json(cleared).findings[0].get("cvss_vector") == ""
