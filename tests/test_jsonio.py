import json

import pytest

from pwndoc_ai.jsonio import (
    JsonFileError,
    finding_key,
    read_findings,
    read_findings_or_empty,
    write_json_atomic,
)

DATA = [{"_id": "a1", "title": "XSS ✓"}, {"title": "SQLi"}]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-le"])
def test_reads_common_encodings(tmp_path, encoding):
    path = tmp_path / "f.json"
    path.write_bytes(json.dumps(DATA, ensure_ascii=False).encode(encoding))
    assert read_findings(path) == DATA


def test_empty_file_is_empty_list(tmp_path):
    path = tmp_path / "f.json"
    path.write_text("  \n")
    assert read_findings(path) == []


def test_missing_file_raises(tmp_path):
    with pytest.raises(JsonFileError):
        read_findings(tmp_path / "nope.json")
    assert read_findings_or_empty(tmp_path / "nope.json") == []


def test_audit_export_with_findings_key(tmp_path):
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"findings": DATA}))
    assert read_findings(path) == DATA


def test_non_json_rejected(tmp_path):
    path = tmp_path / "f.json"
    path.write_bytes(b"\x00\x01 not json")
    with pytest.raises(JsonFileError):
        read_findings(path)


def test_atomic_write_roundtrip_utf8(tmp_path):
    path = tmp_path / "sub" / "out.json"
    write_json_atomic(path, DATA)
    assert json.loads(path.read_text(encoding="utf-8")) == DATA
    assert not list(path.parent.glob("*.tmp"))


def test_finding_key_priority():
    assert finding_key({"_id": "x", "id": "y", "title": "t"}) == "x"
    assert finding_key({"id": "y", "title": "t"}) == "y"
    assert finding_key({"title": " t "}) == "t"


def test_written_files_and_new_dirs_are_private(tmp_path):
    import os
    import stat

    target = tmp_path / "a" / "b" / "out.json"
    write_json_atomic(target, DATA)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "a" / "b").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(tmp_path / "a").st_mode) == 0o700


def test_existing_parent_dir_permissions_untouched(tmp_path):
    import os
    import stat

    tmp_path.chmod(0o755)
    write_json_atomic(tmp_path / "out.json", DATA)
    assert stat.S_IMODE(os.stat(tmp_path).st_mode) == 0o755


def test_finding_keys_disambiguate_duplicate_titles_only_when_needed():
    from pwndoc_ai.jsonio import finding_keys

    findings = [{"title": "XSS"}, {"title": "XSS"}, {"title": "SQLi"}, {"_id": "a", "title": "XSS"}]
    assert finding_keys(findings) == ["XSS#1", "XSS#2", "SQLi", "a"]
    assert finding_keys([{"_id": "a"}, {"_id": "b"}]) == ["a", "b"]
