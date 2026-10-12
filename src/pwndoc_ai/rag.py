"""Optional local-document RAG (ChromaDB with its built-in ONNX embedder, no PyTorch).

Install with ``pip install 'pwndoc-ai[rag]'``. Users upload their own methodology notes,
standards or past reports through the Settings page; files are chunked, embedded and stored
under ``<data_dir>/rag``. Re-uploading a file with the same content is a no-op.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any

CHUNK_CHARS = 1200
CHUNK_OVERLAP = 250
SUPPORTED = {".pdf", ".txt", ".md", ".docx", ".json"}
SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")
MAX_CONTEXT_CHARS = 2000
MAX_NAME_CHARS = 120
MAX_DOCX_UNCOMPRESSED = 100 * 1024 * 1024  # zip-bomb guard
MAX_CHUNKS_PER_FILE = 5000
ADD_BATCH = 500


class RagUnavailable(RuntimeError):
    pass


def safe_filename(name: str) -> str:
    cleaned = SAFE_NAME.sub("_", Path(name).name).strip(". ")
    if len(cleaned) > MAX_NAME_CHARS:
        suffix = Path(cleaned).suffix[:16]
        cleaned = cleaned[: MAX_NAME_CHARS - len(suffix)].rstrip(". ") + suffix
    return cleaned or "document"


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return []
    step = max(1, size - overlap)
    return [text[i : i + size] for i in range(0, len(text), step)]


def _check_docx_size(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            expanded = sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile as exc:
        raise ValueError("not a valid .docx file") from exc
    if expanded > MAX_DOCX_UNCOMPRESSED:
        raise ValueError("document expands to more than 100 MB")


def read_document(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RagUnavailable("pypdf is not installed (pip install 'pwndoc-ai[rag]')") from exc
        return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    if suffix == ".docx":
        _check_docx_size(path)  # before parsing: a zip bomb must never reach python-docx
        try:
            import docx
        except ImportError as exc:
            raise RagUnavailable(
                "python-docx is not installed (pip install 'pwndoc-ai[rag]')"
            ) from exc
        return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    raw = path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".json":
        try:
            return json.dumps(json.loads(raw), indent=1, ensure_ascii=False)
        except ValueError:
            return raw
    return raw


class DocumentIndex:
    def __init__(self, base_dir: str | Path, collection: Any | None = None):
        """``collection`` lets tests inject a fake; normally ChromaDB provides it."""
        self.base = Path(base_dir)
        self.docs_dir = self.base / "docs"
        self.docs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if collection is not None:
            self._collection = collection
            return
        try:
            import chromadb
        except ImportError as exc:
            raise RagUnavailable(
                "chromadb is not installed (pip install 'pwndoc-ai[rag]')"
            ) from exc
        client = chromadb.PersistentClient(path=str(self.base / "chroma"))
        self._collection = client.get_or_create_collection("documents")

    def add_file(self, path: Path) -> int:
        """Index ``path``; returns the number of chunks stored (0 if unchanged/empty)."""
        if path.suffix.lower() not in SUPPORTED:
            raise ValueError(f"unsupported file type: {path.suffix}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        existing = self._collection.get(where={"source": path.name}, limit=1)
        if existing["ids"] and existing["metadatas"][0].get("sha") == digest:
            return 0
        chunks = chunk_text(read_document(path))  # parse first: a bad file keeps the old index
        if len(chunks) > MAX_CHUNKS_PER_FILE:
            raise ValueError(
                f"document is too large ({len(chunks)} chunks, max {MAX_CHUNKS_PER_FILE})"
            )
        self._delete_chunks(path.name)  # replaces stale chunks but never touches the file itself
        for start in range(0, len(chunks), ADD_BATCH):
            batch = chunks[start : start + ADD_BATCH]
            self._collection.add(
                ids=[f"{path.name}:{digest}:{start + i}" for i in range(len(batch))],
                documents=batch,
                metadatas=[{"source": path.name, "sha": digest} for _ in batch],
            )
        return len(chunks)

    def _delete_chunks(self, name: str) -> None:
        self._collection.delete(where={"source": name})

    def remove(self, name: str) -> None:
        """Forget ``name``: drop its chunks and delete the stored file."""
        self._delete_chunks(name)
        (self.docs_dir / name).unlink(missing_ok=True)

    def list_documents(self) -> list[dict[str, object]]:
        metas = self._collection.get(include=["metadatas"])["metadatas"] or []
        counts: dict[str, int] = {}
        for meta in metas:
            counts[meta["source"]] = counts.get(meta["source"], 0) + 1
        return [{"name": name, "chunks": count} for name, count in sorted(counts.items())]

    def query(self, text: str, k: int = 5) -> str:
        if not text.strip() or self._collection.count() == 0:
            return ""
        result = self._collection.query(query_texts=[text], n_results=k)
        docs = result["documents"][0]
        metas = result["metadatas"][0]
        return "\n\n".join(
            f"[{meta['source']}]\n{doc[:MAX_CONTEXT_CHARS]}"
            for doc, meta in zip(docs, metas, strict=True)
        )
