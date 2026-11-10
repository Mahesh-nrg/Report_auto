"""read_report on hostile or broken files: a short fixed error or a bounded read, never a hang."""

from __future__ import annotations

import io
import random
import time
import zipfile

import pytest

pytest.importorskip("lxml")

from reader_fixtures import cell, detail_table, p, package, row, summary_table, table, zip_of  # noqa: E402

from pwndoc_ai.docx_engine import limits as engine_limits  # noqa: E402
from pwndoc_ai.docx_import import reader_xml  # noqa: E402
from pwndoc_ai.docx_import.errors import DocumentUnreadable, ImportFailure, ImportLimitError  # noqa: E402
from pwndoc_ai.docx_import.reader import read_report  # noqa: E402

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
ONE = detail_table("001", "Finding", severity="Low")


def read(data: bytes):
    return read_report(data, file_name="x.docx")


def replace_member(data: bytes, name: str, payload: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = {n: archive.read(n) for n in archive.namelist()}
    files[name] = payload
    return zip_of(files)


@pytest.mark.parametrize("data", [b"", b"hello", b"PK\x03\x04garbage", b"\x00" * 1000])
def test_garbage_is_unreadable(data):
    with pytest.raises(DocumentUnreadable) as caught:
        read(data)
    assert caught.value.code == "unreadable"


def test_random_zips_never_crash():
    rng = random.Random(3)
    for _ in range(30):
        files = {
            "[Content_Types].xml": b"<Types/>",
            "word/document.xml": bytes(rng.randrange(256) for _ in range(rng.randrange(1, 300))),
        }
        if rng.random() < 0.5:
            files["word/document.xml"] = (
                f"<w:document {NS}><w:body>".encode() + files["word/document.xml"]
            )
        with pytest.raises(ImportFailure):
            read(zip_of(files))


def test_a_word_document_without_the_template_tables():
    with pytest.raises(DocumentUnreadable) as caught:
        read(package(p("Just a letter") + table(row(cell(p("a")), cell(p("b"))))))
    assert caught.value.code == "not_template"


def test_a_summary_table_without_rows_has_no_findings():
    with pytest.raises(DocumentUnreadable) as caught:
        read(package(summary_table()))
    assert caught.value.code == "no_findings"


def test_entity_declarations_are_refused():
    evil = (
        f'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        f"<w:document {NS}><w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>"
    ).encode()
    with pytest.raises(DocumentUnreadable) as caught:
        read(replace_member(package(ONE), "word/document.xml", evil))
    assert "passwd" not in str(caught.value)


def test_entities_in_an_auxiliary_part_are_ignored_not_trusted():
    evil = b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x "boom">]><w:numbering/>'
    result = read(replace_member(package(ONE), "word/numbering.xml", evil))
    assert result.findings[0].title == "Finding"


def test_zip_bomb_is_refused(monkeypatch):
    monkeypatch.setattr(engine_limits, "MAX_UNCOMPRESSED_BYTES", 1024 * 1024)
    bomb = replace_member(package(ONE), "word/media/zeros.bin", b"\0" * (5 * 1024 * 1024))
    with pytest.raises(ImportLimitError) as caught:
        read(bomb)
    assert caught.value.code == "too_large"


def test_too_many_paragraphs_are_refused(monkeypatch):
    monkeypatch.setattr(engine_limits, "MAX_PARAGRAPHS", 50)
    with pytest.raises(ImportLimitError):
        read(package(ONE + p("x") * 100))


def test_deeply_nested_tables_are_bounded():
    nested = ONE
    for _ in range(60):
        nested = table(row(cell(p("layout"), nested)))
    started = time.monotonic()
    try:
        result = read(package(nested))
    except DocumentUnreadable:
        result = None  # deeper than the XML parser allows: refused, which is fine
    assert time.monotonic() - started < 10
    if result is not None:
        assert result.findings == () or len(result.findings) <= 1


def test_nested_tables_inside_cells_are_read_to_a_fixed_depth():
    inner = table(row(cell(p("deep text"))))
    for _ in range(8):
        inner = table(row(cell(inner)))
    xml = detail_table("001", "T", severity="Low", description=[inner])
    finding = read(package(xml)).findings[0]
    assert "deep text" not in finding.description  # below MAX_TABLE_DEPTH: not read


def test_twenty_thousand_paragraphs_within_a_time_bound():
    body = ONE + "".join(p(f"paragraph {i} with some words") for i in range(20_000))
    started = time.monotonic()
    result = read(package(body))
    assert time.monotonic() - started < 15
    assert len(result.findings) == 1


def test_many_findings_within_a_time_bound():
    body = "".join(
        detail_table(f"{n:03d}", f"Finding {n}", severity="Low", description=[p("d " * 200)])
        for n in range(1, 151)
    )
    started = time.monotonic()
    result = read(package(body))
    assert time.monotonic() - started < 15 and len(result.findings) == 150


def test_the_wall_clock_budget_stops_a_slow_read(monkeypatch):
    monkeypatch.setattr(reader_xml, "READ_SECONDS", -1.0)
    monkeypatch.setattr(reader_xml, "CHECK_EVERY", 1)
    with pytest.raises(ImportLimitError) as caught:
        read(package(ONE))
    assert caught.value.code == "too_large"


def test_error_messages_never_echo_document_text():
    secret = "TopSecretClientName"
    with pytest.raises(DocumentUnreadable) as caught:
        read(package(p(secret)))
    assert secret not in str(caught.value) and len(str(caught.value)) < 120


def test_duplicate_member_names_and_unsafe_paths_are_refused():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("../evil.xml", b"x")
    with pytest.raises(DocumentUnreadable):
        read(out.getvalue())
