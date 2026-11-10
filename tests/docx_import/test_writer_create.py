"""Writer: the create sequence against a stateful fake PwnDoc, failures and rollback."""

from __future__ import annotations

import pytest
from test_writer_fakes import (
    IMPORT_ID,
    TEMPLATE_ID,
    FakePwnDoc,
    data_url,
    finding,
    image,
    make_plan,
    ok,
    png,
)

from pwndoc_ai.docx_import.errors import (
    ImportCancelled,
    ImportConflict,
    ImportInputError,
    ImportSessionExpired,
    ImportUpstreamError,
)
from pwndoc_ai.docx_import.journal import JournalWriter, read_journal
from pwndoc_ai.docx_import.model import Contact, PocBlock
from pwndoc_ai.docx_import.writer import create_import

HEX = r"[0-9a-f]{24}"
IMG1, IMG2 = png(seed=1), png(seed=2)


def sample_plan(**header):
    findings = [
        finding("f001", "Low thing", severity="Low", source_id="001"),
        finding(
            "f002",
            "Critical thing",
            severity="Critical",
            source_id="002",
            poc=(
                PocBlock(kind="html", html="<p>see</p>"),
                PocBlock(kind="image", image_key="i001"),
            ),
        ),  # fmt: skip
        finding("f003", "Skipped", include=False),
    ]
    images = [
        image("i001", "f002", IMG1, 1),
        image("i002", "f002", IMG1, 2),  # same bytes: one upload
        image("i003", "f001", IMG2, 3),
        image("i004", "f003", png(seed=9), 4),  # its finding is excluded
    ]
    return make_plan(findings, images, **header)


def blobs():
    return {"i001": IMG1, "i002": IMG1, "i003": IMG2, "i004": png(seed=9)}


def run(fake, plan, tmp_path, *, confirm=False, cancelled=lambda: False, steps=None):
    writer = JournalWriter(tmp_path / "journal.json", IMPORT_ID)
    outcome = create_import(
        plan,
        open_client=fake.open_client,
        read_image=blobs().__getitem__,
        journal=writer,
        progress=(lambda *a: steps.append(a)) if steps is not None else (lambda *a: None),
        cancelled=cancelled,
        confirm_duplicate=confirm,
    )
    return outcome, writer


def test_full_create_sequence(tmp_path):
    fake = FakePwnDoc(
        custom_fields=[
            {"_id": "c" * 24, "label": "Vulnerable URL", "display": "finding", "text": []}
        ]
    )
    steps = []
    outcome, writer = run(
        fake, sample_plan(contact=Contact("Bob@Acme.test", "Bob")), tmp_path, steps=steps
    )
    assert outcome.error is None and outcome.rollback is None
    result = outcome.result
    assert result.company_created and result.client_created
    assert (result.images_uploaded, result.images_reused, result.images_skipped) == (2, 0, 1)
    writes = [(m, p.replace(result.audit_id, "A")) for m, p in fake.writes()]
    assert writes == [
        ("POST", "/api/companies"), ("POST", "/api/clients"), ("POST", "/api/audits"),
        ("PUT", "/api/audits/A/general"), ("POST", "/api/images/"), ("POST", "/api/images/"),
        ("POST", "/api/audits/A/findings"), ("POST", "/api/audits/A/findings"),
    ]  # fmt: skip
    general = next(b for m, p, b in fake.bodies if m == "PUT")
    assert general == {
        "company": {"_id": result.company_id}, "template": TEMPLATE_ID,
        "scope": ["https://brandarmy.com", "https://wink.ing/"], "date_start": "2026-03-05",
        "client": result.client_id,
    }  # fmt: skip
    audit = fake.audits[result.audit_id]
    assert [f["title"] for f in audit["findings"]] == ["Critical thing", "Low thing"]
    first_poc = audit["findings"][0]["poc"]
    image_ids = [i for i, row in fake.images.items() if row["auditId"] == result.audit_id]
    assert first_poc.startswith("<p>see</p><img src=") and first_poc.count("<img ") == 2
    assert all(i in first_poc for i in image_ids[:1])
    assert list(result.finding_ids) == [f["_id"] for f in audit["findings"]]
    journal = read_journal(tmp_path / "journal.json")
    assert journal.state == "complete"
    kinds = ["company", "client", "audit", "image", "image", "finding", "finding"]
    assert [o.kind for o in journal.objects] == kinds
    labels = {"Checking PwnDoc", "Creating the audit", "Uploading screenshots", "Verifying"}
    assert {s[0] for s in steps} >= labels
    assert steps[-1][1] == steps[-1][2]
    assert writer.current.state == "complete"


def test_existing_company_and_contact_are_reused_not_journaled(tmp_path):
    fake = FakePwnDoc()
    fake.companies = [{"_id": "2" * 24, "name": "BRANDARMY"}]
    fake.clients = [{"_id": "3" * 24, "email": "bob@acme.test"}]
    outcome, _ = run(fake, sample_plan(contact=Contact("Bob@Acme.test")), tmp_path)
    assert outcome.result.company_id == "2" * 24 and not outcome.result.company_created
    assert outcome.result.client_id == "3" * 24 and not outcome.result.client_created
    kinds = [o.kind for o in read_journal(tmp_path / "journal.json").objects]
    assert "company" not in kinds and "client" not in kinds
    assert ("POST", "/api/companies") not in fake.calls


def test_company_created_concurrently_is_reused_after_a_422(tmp_path):
    fake = FakePwnDoc()
    original = fake._create_company

    def racing(body):
        fake.companies.append({"_id": "4" * 24, "name": "Brandarmy"})
        return original(body)

    fake._create_company = racing
    fake.companies = []
    outcome, _ = run(fake, sample_plan(), tmp_path)
    # the fake lists no company at check time, then refuses the create as a duplicate
    assert outcome.result.company_id == "4" * 24 and not outcome.result.company_created


def test_image_owned_by_another_audit_is_reused_and_never_journaled(tmp_path):
    fake = FakePwnDoc()
    fake.images["9" * 24] = {"auditId": "8" * 24, "value": data_url(IMG1), "name": "old.png"}
    outcome, _ = run(fake, sample_plan(), tmp_path)
    assert (outcome.result.images_uploaded, outcome.result.images_reused) == (1, 1)
    journal = read_journal(tmp_path / "journal.json")
    assert "9" * 24 not in {o.id for o in journal.objects}
    assert "9" * 24 in fake.audits[outcome.result.audit_id]["findings"][0]["poc"]


def test_duplicate_audit_is_refused_unless_confirmed(tmp_path):
    fake = FakePwnDoc()
    fake.companies = [{"_id": "2" * 24, "name": "Brandarmy"}]
    fake.add_audit("BrandArmy", "2" * 24)
    outcome, _ = run(fake, sample_plan(), tmp_path)
    assert isinstance(outcome.error, ImportConflict) and outcome.error.code == "duplicate_audit"
    assert fake.writes() == [] and outcome.rollback is None
    assert not (tmp_path / "journal.json").exists()
    confirmed, _ = run(fake, sample_plan(), tmp_path / "again", confirm=True)
    assert confirmed.result is not None


@pytest.mark.parametrize(
    "header",
    [{"template_id": "6" * 24}, {"language": "fr"}, {"audit_type": "Nope"}, {"company": " "}],
)
def test_local_blockers_make_no_write(tmp_path, header):
    fake = FakePwnDoc()
    outcome, _ = run(fake, sample_plan(**header), tmp_path)
    assert isinstance(outcome.error, ImportInputError) and outcome.error.code == "blockers"
    assert fake.writes() == []


FAILURES = [
    ("POST", "/api/companies", 500, 1, ImportUpstreamError),
    ("POST", "/api/clients", 500, 1, ImportUpstreamError),
    ("POST", "/api/audits", 500, 1, ImportUpstreamError),
    ("PUT", f"/api/audits/{HEX}/general", 500, 1, ImportUpstreamError),
    ("POST", "/api/images/", 500, 2, ImportUpstreamError),
    ("GET", f"/api/images/{HEX}", 401, 1, ImportSessionExpired),
    ("POST", f"/api/audits/{HEX}/findings", 401, 2, ImportSessionExpired),
    ("GET", f"/api/audits/{HEX}", 500, 1, ImportUpstreamError),
]


@pytest.mark.parametrize(("method", "pattern", "status", "nth", "error"), FAILURES)
def test_failure_at_each_step_rolls_back_exactly_the_journal(
    tmp_path, method, pattern, status, nth, error
):
    fake = FakePwnDoc(admin=True)
    fake.companies = [{"_id": "1" * 24, "name": "Unrelated"}]
    keep = fake.add_audit("Keep me", "1" * 24)
    fake.fail(method, pattern, status, nth)
    outcome, writer = run(fake, sample_plan(contact=Contact("bob@acme.test")), tmp_path)
    assert isinstance(outcome.error, error) and outcome.result is None
    journal = writer.current
    deleted = {(i.kind, i.id) for i in outcome.rollback.items} if outcome.rollback else set()
    assert deleted == {(o.kind, o.id) for o in journal.objects}
    assert list(fake.audits) == [keep]
    assert [c["_id"] for c in fake.companies] == ["1" * 24]
    assert fake.clients == []
    # a failed ownership read still journals the upload, so nothing is left behind (COR-4)
    assert fake.images == {}
    if journal.objects:
        assert read_journal(tmp_path / "journal.json").state == "rolled_back"


def test_rollback_that_cannot_delete_is_partial(tmp_path):
    fake = FakePwnDoc()
    fake.fail("POST", f"/api/audits/{HEX}/findings", 401, 1)
    fake.fail("DELETE", f"/api/audits/{HEX}", 401, 1)
    outcome, _ = run(fake, sample_plan(), tmp_path)
    assert isinstance(outcome.error, ImportSessionExpired)
    outcomes = {i.kind: i.outcome for i in outcome.rollback.items}
    assert outcomes["audit"] == "failed" and outcomes["company"] == "failed"
    assert outcomes["image"] == "failed"  # the audit still shows it: kept for a retry
    assert read_journal(tmp_path / "journal.json").state == "partial"


def test_non_admin_rollback_with_kept_images_is_rolled_back(tmp_path):
    fake = FakePwnDoc()
    fake.fail("POST", f"/api/audits/{HEX}/findings", 500, 1)
    outcome, _ = run(fake, sample_plan(), tmp_path)
    assert outcome.rollback.count("kept", "image") == 2
    assert read_journal(tmp_path / "journal.json").state == "rolled_back"
    # a non-admin cannot see every audit, so the company it created is kept, never risked
    assert fake.audits == {} and len(fake.companies) == 1
    assert outcome.rollback.count("kept", "company") == 1


def test_cancel_midway_rolls_back(tmp_path):
    fake = FakePwnDoc(admin=True)
    calls = []

    def cancelled():
        calls.append(1)
        return len(calls) > 5  # after the audit exists

    outcome, _ = run(fake, sample_plan(), tmp_path, cancelled=cancelled)
    assert isinstance(outcome.error, ImportCancelled)
    assert outcome.rollback is not None and fake.audits == {} and fake.companies == []


def test_cancel_before_any_write_needs_no_rollback(tmp_path):
    fake = FakePwnDoc()
    outcome, _ = run(fake, sample_plan(), tmp_path, cancelled=lambda: True)
    assert isinstance(outcome.error, ImportCancelled) and outcome.rollback is None
    assert fake.calls == []


def test_verify_count_mismatch_rolls_back(tmp_path):
    fake = FakePwnDoc(admin=True)
    original = fake._create_finding

    def drops_one(body, audit_id):
        if body["title"] == "Low thing":
            return ok("Audit Finding created successfully")  # answers success, stores nothing
        return original(body, audit_id)

    fake._create_finding = drops_one
    outcome, _ = run(fake, sample_plan(), tmp_path)
    assert isinstance(outcome.error, ImportUpstreamError)
    assert fake.audits == {}
    assert any(i.kind == "finding" for i in outcome.rollback.items)


def test_tampered_image_bytes_fail_and_roll_back(tmp_path):
    fake = FakePwnDoc(admin=True)
    writer = JournalWriter(tmp_path / "journal.json", IMPORT_ID)
    outcome = create_import(
        sample_plan(),
        open_client=fake.open_client,
        read_image=lambda key: b"\x89PNG tampered",
        journal=writer,
        progress=lambda *a: None,
        cancelled=lambda: False,
        confirm_duplicate=False,
    )
    assert isinstance(outcome.error, ImportInputError) and outcome.error.code == "image_not_usable"
    assert fake.audits == {} and ("POST", "/api/images/") not in fake.calls
