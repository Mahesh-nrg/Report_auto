"""Undo journal: atomic 0600 writes, strict reads, every record survives a crash."""

from __future__ import annotations

import json
import os
import stat

import pytest

from pwndoc_ai.docx_import import journal as journal_mod
from pwndoc_ai.docx_import import limits
from pwndoc_ai.docx_import.errors import ImportInputError, ImportLimitError
from pwndoc_ai.docx_import.journal import JournalWriter, read_journal, write_journal
from pwndoc_ai.docx_import.records import Journal

IMPORT_ID = "0123456789ab4def8123456789abcdef"
AID = "a" * 24


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_nothing_is_written_before_the_first_record(tmp_path):
    path = tmp_path / "imp" / "journal.json"
    writer = JournalWriter(path, IMPORT_ID)
    assert not path.exists()
    assert writer.current.state == "open" and writer.current.objects == ()
    assert read_journal(path) is None


def test_each_record_is_on_disk_before_the_next_call(tmp_path):
    path = tmp_path / "imp" / "journal.json"
    writer = JournalWriter(path, IMPORT_ID)
    writer.record("company", "c" * 24)
    assert [o.kind for o in read_journal(path).objects] == ["company"]
    writer.record("audit", AID)
    writer.record("image", "1" * 24, AID)
    on_disk = read_journal(path)
    assert [(o.kind, o.id, o.audit_id) for o in on_disk.objects] == [
        ("company", "c" * 24, ""), ("audit", AID, ""), ("image", "1" * 24, AID),
    ]  # fmt: skip
    assert mode(path) == 0o600
    assert mode(path.parent) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["journal.json"]  # no temp files left


def test_record_is_idempotent_and_validates(tmp_path):
    writer = JournalWriter(tmp_path / "j.json", IMPORT_ID)
    writer.record("audit", AID)
    writer.record("audit", AID)
    assert len(writer.current.objects) == 1
    with pytest.raises(ImportInputError):
        writer.record("finding", "f" * 24)  # findings need their audit id
    with pytest.raises(ImportInputError):
        writer.record("audit", "../etc")
    with pytest.raises(ImportInputError):
        writer.record("user", "b" * 24)


def test_set_state_and_resume(tmp_path):
    path = tmp_path / "j.json"
    writer = JournalWriter(path, IMPORT_ID)
    writer.record("audit", AID)
    writer.set_state("complete")
    again = JournalWriter(path, IMPORT_ID)
    assert again.current.state == "complete"
    assert [o.id for o in again.current.objects] == [AID]
    with pytest.raises(ImportInputError):
        writer.set_state("bogus")


def test_a_journal_of_another_import_is_refused(tmp_path):
    path = tmp_path / "j.json"
    JournalWriter(path, IMPORT_ID).record("audit", AID)
    with pytest.raises(ImportInputError):
        JournalWriter(path, "fedcba9876544def8123456789abcdef")


def test_memory_is_updated_even_when_the_disk_write_fails(tmp_path, monkeypatch):
    writer = JournalWriter(tmp_path / "j.json", IMPORT_ID)

    def broken(path, journal):
        raise OSError("disk full")

    monkeypatch.setattr(journal_mod, "write_journal", broken)
    with pytest.raises(OSError):
        writer.record("audit", AID)
    assert [o.id for o in writer.current.objects] == [AID]  # rollback can still see it


def test_failed_write_keeps_the_previous_file_and_no_temp(tmp_path, monkeypatch):
    path = tmp_path / "j.json"
    writer = JournalWriter(path, IMPORT_ID)
    writer.record("audit", AID)

    def crash(src, dst):
        raise OSError("crash during replace")

    monkeypatch.setattr(journal_mod.os, "replace", crash)
    with pytest.raises(OSError):
        writer.record("company", "c" * 24)
    assert [o.kind for o in read_journal(path).objects] == ["audit"]
    assert [p.name for p in tmp_path.iterdir()] == ["j.json"]


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"\xff\xfe",
        b"[]",
        json.dumps(
            {
                "version": 2,
                "import_id": IMPORT_ID,
                "state": "open",
                "updated": "2026-01-01T00:00:00Z",
                "objects": [],
            }
        ).encode(),
        json.dumps(
            {
                "version": 1,
                "import_id": IMPORT_ID,
                "state": "open",
                "updated": "2026-01-01T00:00:00Z",
                "objects": [],
                "x": 1,
            }
        ).encode(),
        json.dumps(
            {
                "version": 1,
                "import_id": IMPORT_ID,
                "state": "open",
                "updated": "2026-01-01T00:00:00Z",
                "objects": [
                    {"kind": "audit", "id": AID, "at": "2026-01-01T00:00:00Z", "audit_id": ""}
                ]
                * 2,
            }
        ).encode(),
    ],
)
def test_corrupt_journal_is_refused(tmp_path, content):
    path = tmp_path / "j.json"
    path.write_bytes(content)
    with pytest.raises(ImportInputError):
        read_journal(path)


def test_oversized_journal_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "j.json"
    path.write_bytes(b" " * 100)
    monkeypatch.setattr(journal_mod, "MAX_JOURNAL_BYTES", 10)
    with pytest.raises(ImportInputError):
        read_journal(path)


def test_symlinked_journal_is_refused(tmp_path):
    real = tmp_path / "real.json"
    write_journal(real, Journal(import_id=IMPORT_ID, state="open", updated="2026-01-01T00:00:00Z"))
    link = tmp_path / "j.json"
    link.symlink_to(real)
    with pytest.raises(ImportInputError):
        read_journal(link)


def test_object_cap(tmp_path, monkeypatch):
    writer = JournalWriter(tmp_path / "j.json", IMPORT_ID)
    monkeypatch.setattr(limits, "MAX_JOURNAL_OBJECTS", 1)
    writer.record("audit", AID)
    with pytest.raises(ImportLimitError):
        writer.record("company", "c" * 24)


def test_journal_holds_ids_only(tmp_path):
    path = tmp_path / "j.json"
    writer = JournalWriter(path, IMPORT_ID)
    writer.record("audit", AID)
    data = json.loads(path.read_text())
    assert set(data) == {"version", "import_id", "state", "updated", "objects"}
    assert set(data["objects"][0]) == {"kind", "id", "at", "audit_id"}
