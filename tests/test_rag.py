import json

import pytest

from pwndoc_ai.rag import CHUNK_CHARS, CHUNK_OVERLAP, chunk_text, read_document, safe_filename


def test_chunk_text_empty():
    assert chunk_text("  \n ") == []


def test_chunk_text_short_is_single_chunk():
    assert chunk_text("hello   world") == ["hello world"]


def test_chunk_text_overlaps():
    text = "".join(chr(97 + i % 26) for i in range(3000))
    chunks = chunk_text(text, size=1000, overlap=200)
    assert all(len(c) <= 1000 for c in chunks)
    assert chunks[0][-200:] == chunks[1][:200]
    assert "".join(c[:800] for c in chunks[:-1]) + chunks[-1] == text


def test_chunk_defaults_sane():
    assert CHUNK_OVERLAP < CHUNK_CHARS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("my report (final).pdf", "my report _final_.pdf"),
        ("....", "document"),
        ("", "document"),
        ("ok-name_1.md", "ok-name_1.md"),
    ],
)
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_read_document_text_and_markdown(tmp_path):
    for name in ("a.txt", "b.md"):
        path = tmp_path / name
        path.write_text("# Title\nbody", encoding="utf-8")
        assert read_document(path) == "# Title\nbody"


def test_read_document_json_is_pretty_printed(tmp_path):
    path = tmp_path / "d.json"
    path.write_text(json.dumps({"cwe": "CWE-79"}), encoding="utf-8")
    out = read_document(path)
    assert "CWE-79" in out and "\n" in out


def test_read_document_invalid_json_falls_back_to_raw(tmp_path):
    path = tmp_path / "d.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_document(path) == "{not json"


def test_document_index_roundtrip(tmp_path):
    pytest.importorskip("chromadb")
    from pwndoc_ai.rag import DocumentIndex

    index = DocumentIndex(tmp_path / "rag")
    doc = index.docs_dir / "notes.txt"
    doc.write_text("Stored XSS happens when user input is persisted and rendered unescaped. " * 5)
    assert index.add_file(doc) > 0
    assert index.add_file(doc) == 0  # unchanged content is a no-op
    assert index.list_documents()[0]["name"] == "notes.txt"
    assert "notes.txt" in index.query("stored xss", k=1)
    index.remove("notes.txt")
    assert index.list_documents() == []


def test_document_index_rejects_unsupported_type(tmp_path):
    pytest.importorskip("chromadb")
    from pwndoc_ai.rag import DocumentIndex

    index = DocumentIndex(tmp_path / "rag")
    bad = tmp_path / "x.exe"
    bad.write_bytes(b"MZ")
    with pytest.raises(ValueError):
        index.add_file(bad)


# -- DocumentIndex logic against an in-memory collection (no chromadb needed) --------------------
class FakeCollection:
    def __init__(self):
        self.rows: dict[str, tuple[str, dict]] = {}

    def add(self, ids, documents, metadatas):
        for rid, doc, meta in zip(ids, documents, metadatas, strict=True):
            self.rows.setdefault(rid, (doc, meta))  # chroma ignores duplicate ids

    def _match(self, where):
        return [
            (i, d, m)
            for i, (d, m) in self.rows.items()
            if all(m.get(k) == v for k, v in where.items())
        ]

    def get(self, where=None, limit=None, include=None):
        rows = self._match(where) if where else [(i, d, m) for i, (d, m) in self.rows.items()]
        rows = rows[:limit] if limit else rows
        return {"ids": [r[0] for r in rows], "metadatas": [r[2] for r in rows]}

    def delete(self, where):
        for rid, _, _ in self._match(where):
            del self.rows[rid]

    def count(self):
        return len(self.rows)

    def query(self, query_texts, n_results):
        rows = list(self.rows.values())[:n_results]
        return {"documents": [[d for d, _ in rows]], "metadatas": [[m for _, m in rows]]}


@pytest.fixture
def index(tmp_path):
    from pwndoc_ai.rag import DocumentIndex

    return DocumentIndex(tmp_path / "rag", collection=FakeCollection())


def stored(index, name, text):
    path = index.docs_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def test_add_file_keeps_the_uploaded_file_on_disk(index):
    path = stored(index, "notes.txt", "stored xss notes " * 100)
    assert index.add_file(path) > 0
    assert path.exists()  # regression: indexing used to unlink the file it was about to read


def test_unchanged_content_is_a_noop_and_changed_content_replaces_chunks(index):
    path = stored(index, "notes.txt", "alpha " * 500)
    first = index.add_file(path)
    assert index.add_file(path) == 0
    path.write_text("beta " * 50, encoding="utf-8")
    second = index.add_file(path)
    assert 0 < second < first
    assert index.list_documents() == [{"name": "notes.txt", "chunks": second}]


def test_identical_content_under_two_names_is_indexed_for_both(index):
    for name in ("a.txt", "b.txt"):
        stored(index, name, "same text " * 200)
        assert index.add_file(index.docs_dir / name) > 0
    names = {d["name"] for d in index.list_documents()}
    assert names == {"a.txt", "b.txt"}


def test_remove_drops_chunks_and_file(index):
    path = stored(index, "gone.md", "x " * 400)
    index.add_file(path)
    index.remove("gone.md")
    assert index.list_documents() == [] and not path.exists()


def test_empty_document_adds_nothing(index):
    assert index.add_file(stored(index, "empty.txt", "   \n ")) == 0
    assert index.list_documents() == []


def test_oversized_document_is_rejected_and_old_index_kept(index, monkeypatch):
    from pwndoc_ai import rag

    path = stored(index, "big.txt", "ok " * 300)
    index.add_file(path)
    before = index.list_documents()
    monkeypatch.setattr(rag, "MAX_CHUNKS_PER_FILE", 2)
    path.write_text("word " * 5000, encoding="utf-8")
    with pytest.raises(ValueError, match="too large"):
        index.add_file(path)
    assert index.list_documents() == before


def test_chunks_are_added_in_batches(index, monkeypatch):
    from pwndoc_ai import rag

    calls = []
    original = index._collection.add
    index._collection.add = lambda **kw: (calls.append(len(kw["ids"])), original(**kw))[1]
    monkeypatch.setattr(rag, "ADD_BATCH", 3)
    n = index.add_file(stored(index, "b.txt", "w" * 4000))
    assert sum(calls) == n and all(c <= 3 for c in calls) and len(calls) > 1


def test_unsupported_type_and_query_formatting(index):
    bad = index.docs_dir / "x.exe"
    bad.write_bytes(b"MZ")
    with pytest.raises(ValueError):
        index.add_file(bad)
    assert index.query("anything") == ""  # empty index
    index.add_file(stored(index, "n.txt", "csrf tokens " * 50))
    assert index.query("csrf", k=1).startswith("[n.txt]\n")
    assert index.query("   ") == ""


def test_safe_filename_length_cap_keeps_suffix():
    out = safe_filename("a" * 400 + ".pdf")
    assert len(out) <= 120 and out.endswith(".pdf")


def test_docx_zip_bomb_and_garbage_are_rejected(tmp_path, monkeypatch):
    import zipfile

    from pwndoc_ai import rag

    bomb = tmp_path / "bomb.docx"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "0" * 10_000)
    monkeypatch.setattr(rag, "MAX_DOCX_UNCOMPRESSED", 1000)
    with pytest.raises(ValueError, match="expands"):
        read_document(bomb)
    junk = tmp_path / "junk.docx"
    junk.write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="valid"):
        read_document(junk)
