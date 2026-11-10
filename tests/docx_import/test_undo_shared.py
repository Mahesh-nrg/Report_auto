"""Undo never deletes what someone else depends on (SEC-1/2/3, COR-1/2).

A contact, company or screenshot the import created is deleted only when an administrator's
scan of all of PwnDoc finds nothing else naming its id; an audit that gained hand-written
findings is not deleted at all.
"""

from __future__ import annotations

import pytest
from test_undo import full_journal, seeded
from test_writer_fakes import FakePwnDoc

from pwndoc_ai.docx_import.errors import ImportConflict
from pwndoc_ai.docx_import.undo import undo_import
from pwndoc_ai.docx_import.undo_usage import MAX_SCAN_AUDITS, Usage, scan_usage


def outcomes(fake: FakePwnDoc, ids: dict, **kw) -> dict[str, str]:
    result = undo_import(
        full_journal(ids), open_client=fake.open_client, progress=lambda *a: None, **kw
    )
    return {i.kind: i.outcome for i in result.items}


def deletes(fake: FakePwnDoc) -> list[str]:
    return [path for method, path in fake.calls if method == "DELETE"]


def test_non_admin_keeps_company_contact_and_images():
    fake, ids = seeded(admin=False)
    found = outcomes(fake, ids)
    assert found == {"audit": "deleted", "finding": "deleted", "client": "kept",
                     "company": "kept", "image": "kept"}  # fmt: skip
    assert deletes(fake) == [f"/api/audits/{ids['audit']}"]
    assert ids["company"] in {c["_id"] for c in fake.companies}
    assert ids["client"] in {c["_id"] for c in fake.clients}


def test_admin_keeps_a_contact_another_audit_uses():
    fake, ids = seeded(admin=True)
    fake.audits[ids["unrelated"]]["client"] = ids["client"]
    found = outcomes(fake, ids)
    assert found["client"] == "kept" and found["company"] == "deleted"
    assert ids["client"] in {c["_id"] for c in fake.clients}


def test_admin_keeps_a_company_another_contact_links_to():
    fake, ids = seeded(admin=True)
    fake.clients.append({"_id": fake.new_id(), "email": "x@y.z", "company": ids["company"]})
    assert outcomes(fake, ids)["company"] == "kept"


def test_admin_keeps_a_screenshot_another_audit_shows():
    fake, ids = seeded(admin=True)
    poc = f'<p>step</p><img src="{ids["image"]}" alt="Figure 1">'
    fake.audits[ids["unrelated"]]["findings"] = [{"_id": fake.new_id(), "poc": poc}]
    found = outcomes(fake, ids)
    assert found["image"] == "kept" and ids["image"] in fake.images


def test_admin_keeps_a_screenshot_the_library_shows():
    fake, ids = seeded(admin=True)
    fake.vulnerabilities = [
        {"_id": fake.new_id(), "details": [{"poc": f'<img src="{ids["image"]}">'}]}
    ]
    assert outcomes(fake, ids)["image"] == "kept"


def test_unreadable_audit_makes_every_shared_object_kept():
    fake, ids = seeded(admin=True)
    fake.fail("GET", f"/api/audits/{ids['unrelated']}", 500)
    found = outcomes(fake, ids, check_changes=False)
    assert found["audit"] == "deleted"
    assert {found["client"], found["company"], found["image"]} == {"kept"}


def test_too_many_audits_is_unknown_usage():
    fake, _ = seeded(admin=True)
    for _ in range(MAX_SCAN_AUDITS + 1):
        fake.add_audit("bulk")
    with fake.open_client() as client:
        usage = scan_usage(client, skip_audits=frozenset(), skip_clients=frozenset())
    assert usage == Usage() and usage.in_use("a" * 24)


def test_failed_audit_delete_touches_nothing_else():
    fake, ids = seeded(admin=True)
    fake.fail("DELETE", f"/api/audits/{ids['audit']}", 500)
    found = outcomes(fake, ids)
    assert {found["client"], found["company"], found["image"]} == {"failed"}
    assert deletes(fake) == [f"/api/audits/{ids['audit']}"]


def test_audit_with_a_hand_written_finding_is_refused():
    fake, ids = seeded(admin=True)
    fake.audits[ids["audit"]]["findings"].append({"_id": "ab" * 12, "identifier": 99})
    with pytest.raises(ImportConflict) as caught:
        outcomes(fake, ids)
    assert caught.value.code == "changed_since_import"
    assert deletes(fake) == [] and ids["audit"] in fake.audits


def test_the_writer_rollback_skips_the_change_check():
    fake, ids = seeded(admin=True)
    fake.audits[ids["audit"]]["findings"].append({"_id": "ab" * 12, "identifier": 2})
    assert outcomes(fake, ids, check_changes=False)["audit"] == "deleted"


def test_an_audit_already_gone_passes_the_change_check():
    fake, ids = seeded(admin=True)
    del fake.audits[ids["audit"]]
    assert outcomes(fake, ids)["audit"] == "gone"
