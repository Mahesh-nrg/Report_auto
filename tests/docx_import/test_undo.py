"""Undo: safe order, 403 image kept, 404 gone, nothing outside the journal touched."""

from __future__ import annotations

import pytest
from test_writer_fakes import IMPORT_ID, FakePwnDoc

from pwndoc_ai.docx_import.records import CreatedObject, Journal
from pwndoc_ai.docx_import.undo import fully_undone, outcome_for, undo_import
from pwndoc_ai.pwndoc_client import PwnDocError

STAMP = "2026-10-23T10:00:00Z"


def journal(*objects) -> Journal:
    return Journal(
        import_id=IMPORT_ID,
        state="complete",
        updated=STAMP,
        objects=tuple(CreatedObject(k, i, STAMP, a) for k, i, a in objects),
    )


def seeded(admin: bool = False):
    fake = FakePwnDoc(admin=admin)
    company = {"_id": fake.new_id(), "name": "Acme"}
    other_company = {"_id": fake.new_id(), "name": "Other"}
    fake.companies = [company, other_company]
    contact = {"_id": fake.new_id(), "email": "a@b.c"}
    fake.clients = [contact]
    audit = fake.add_audit("Q1", company["_id"])
    fake.audits[audit]["findings"] = [{"_id": fake.new_id(), "identifier": 1}]
    finding = fake.audits[audit]["findings"][0]["_id"]
    image = fake.new_id()
    fake.images[image] = {"auditId": audit, "value": "v", "name": "i001.png"}
    unrelated = fake.add_audit("Keep me", other_company["_id"])
    ids = {"company": company["_id"], "client": contact["_id"], "audit": audit,
           "finding": finding, "image": image, "unrelated": unrelated}  # fmt: skip
    return fake, ids


def full_journal(ids):
    return journal(
        ("company", ids["company"], ""),
        ("client", ids["client"], ""),
        ("audit", ids["audit"], ""),
        ("image", ids["image"], ids["audit"]),
        ("finding", ids["finding"], ids["audit"]),
    )


def test_order_and_outcomes_for_an_admin():
    fake, ids = seeded(admin=True)
    steps = []
    result = undo_import(
        full_journal(ids), open_client=fake.open_client, progress=lambda *a: steps.append(a)
    )
    deletes = [c for c in fake.calls if c[0] == "DELETE"]
    assert deletes == [
        ("DELETE", f"/api/audits/{ids['audit']}"),
        ("DELETE", f"/api/clients/{ids['client']}"),
        ("DELETE", f"/api/companies/{ids['company']}"),
        ("DELETE", f"/api/images/{ids['image']}"),
    ]
    assert {(i.kind, i.outcome) for i in result.items} == {
        ("audit", "deleted"), ("finding", "deleted"), ("client", "deleted"),
        ("company", "deleted"), ("image", "deleted"),
    }  # fmt: skip
    assert fully_undone(result)
    assert ids["unrelated"] in fake.audits  # never touched
    assert [s[0] for s in steps][:4] == [
        "Deleting the audit", "Deleting the contact", "Deleting the company", "Deleting screenshots",
    ]  # fmt: skip


def test_image_403_is_kept_for_a_non_admin():
    fake, ids = seeded(admin=False)
    result = undo_import(full_journal(ids), open_client=fake.open_client, progress=lambda *a: None)
    outcomes = {i.kind: i.outcome for i in result.items}
    assert outcomes["image"] == "kept"
    assert result.count("kept", "image") == 1
    assert fully_undone(result)
    assert ids["image"] in fake.images


def test_404_is_gone_and_a_retry_is_idempotent():
    fake, ids = seeded(admin=True)
    entries = full_journal(ids)
    undo_import(entries, open_client=fake.open_client, progress=lambda *a: None)
    again = undo_import(entries, open_client=fake.open_client, progress=lambda *a: None)
    assert {i.outcome for i in again.items} == {"gone"}
    assert fully_undone(again)


def test_failed_audit_delete_keeps_the_company_and_fails_findings():
    fake, ids = seeded(admin=True)
    fake.fail("DELETE", f"/api/audits/{ids['audit']}", 500)
    result = undo_import(full_journal(ids), open_client=fake.open_client, progress=lambda *a: None)
    outcomes = {i.kind: i.outcome for i in result.items}
    assert outcomes["audit"] == "failed" and outcomes["finding"] == "failed"
    assert outcomes["company"] == "failed"
    assert ids["company"] in {c["_id"] for c in fake.companies}
    assert not fully_undone(result)


def test_company_still_used_by_another_audit_is_kept():
    fake, ids = seeded(admin=True)
    fake.add_audit("Someone else's", ids["company"])
    result = undo_import(full_journal(ids), open_client=fake.open_client, progress=lambda *a: None)
    assert {i.kind: i.outcome for i in result.items}["company"] == "kept"
    assert ids["company"] in {c["_id"] for c in fake.companies}


def test_image_now_owned_by_another_audit_is_kept():
    fake, ids = seeded(admin=True)
    fake.images[ids["image"]]["auditId"] = ids["unrelated"]
    result = undo_import(full_journal(ids), open_client=fake.open_client, progress=lambda *a: None)
    assert {i.kind: i.outcome for i in result.items}["image"] == "kept"
    assert ids["image"] in fake.images


def test_only_journaled_ids_are_touched():
    fake, ids = seeded(admin=True)
    entries = journal(("audit", ids["audit"], ""), ("finding", ids["finding"], ids["audit"]))
    result = undo_import(entries, open_client=fake.open_client, progress=lambda *a: None)
    assert [c for c in fake.calls if c[0] == "DELETE"] == [
        ("DELETE", f"/api/audits/{ids['audit']}")
    ]
    assert [(i.kind, i.outcome) for i in result.items] == [
        ("audit", "deleted"),
        ("finding", "deleted"),
    ]
    assert ids["company"] in {c["_id"] for c in fake.companies}
    assert ids["image"] in fake.images


def test_401_midway_is_failed_not_raised():
    fake, ids = seeded(admin=True)
    fake.fail("DELETE", f"/api/clients/{ids['client']}", 401)
    result = undo_import(full_journal(ids), open_client=fake.open_client, progress=lambda *a: None)
    assert {i.kind: i.outcome for i in result.items}["client"] == "failed"
    assert {i.kind: i.outcome for i in result.items}["audit"] == "deleted"


def test_unreachable_pwndoc_reports_every_item_failed():
    def open_client():
        raise PwnDocError("cannot reach PwnDoc")

    fake, ids = seeded()
    result = undo_import(full_journal(ids), open_client=open_client, progress=lambda *a: None)
    assert len(result.items) == 5 and {i.outcome for i in result.items} == {"failed"}
    assert result.items[0].kind == "audit"


def test_empty_journal_makes_no_call():
    calls = []
    result = undo_import(journal(), open_client=lambda: calls.append(1), progress=lambda *a: None)
    assert result.items == () and calls == []


@pytest.mark.parametrize(
    ("status", "kind", "outcome"),
    [(404, "audit", "gone"), (403, "image", "kept"), (403, "audit", "failed"),
     (401, "image", "failed"), (500, "company", "failed"), (None, "client", "failed")],
)  # fmt: skip
def test_outcome_mapping(status, kind, outcome):
    assert outcome_for(PwnDocError("x", status), kind) == outcome
