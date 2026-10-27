"""Optional local-document RAG (ChromaDB, no PyTorch).

Install with ``pip install 'pwndoc-ai[rag]'``. Users upload their own methodology notes,
standards or past reports through the Settings page; files are chunked, embedded and stored
under ``<data_dir>/rag``.

Indexing is incremental, so that nothing is ever embedded twice:

* a file whose SHA-256 is already stored under the same name is skipped without being parsed;
* the same bytes under another name are a duplicate: no chunk is added;
* a chunk is stored under ``<source>:<sha256 of its text>``; indexing a changed file embeds only
  the chunks that are not stored yet and deletes the stale ones afterwards. A run that is stopped
  half way (error or cancel) leaves its finished chunks behind and resumes from them;
* chunks of the earlier id format (``<name>:<filesha>:<i>``) are left alone while the file is the
  same.

A document that cannot be indexed (no readable text, a duplicate of another file, too large) is
remembered with the hash of its bytes in ``outcomes.json`` next to the index, so it is not parsed
(or OCR'd) again on every run and the file list can say why it is not indexed.

Parsing is bounded: a file of a text type, a PDF page count and the text that is kept are all
capped before the whole document is held in memory, and the OCR tools run under resource limits.

Embeddings come from a small local Ollama model that is started only while it is needed and
stopped when idle (see :mod:`pwndoc_ai.embedder`), or, when Ollama is not installed, from
ChromaDB's built-in ONNX embedder. Both produce the same vectors (space ``minilm-l6-v2``) and
share the collection ``documents``; a model of another vector space gets a collection of its own.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess  # noqa: S404 - only used to run fixed OCR tools with argv lists
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .embedder import MINILM_SPACE, EmbedderError
from .ocr_run import DiskBudgetExceeded, run_limited
from .rag_outcomes import Outcome, OutcomeStore
from .rag_types import (  # noqa: F401 - these names are part of this module's API
    Chunk,
    DocumentError,
    FileResult,
    FileStatus,
    IndexBusy,
    IndexCancelled,
    RagUnavailable,
    SyncResult,
    format_chunks,
)

log = logging.getLogger(__name__)

CHUNK_CHARS = 1200
CHUNK_OVERLAP = 250
SUPPORTED = {".pdf", ".txt", ".md", ".docx", ".json"}
SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")
MAX_CONTEXT_CHARS = 2000
MAX_NAME_CHARS = 120
MAX_DOCX_UNCOMPRESSED = 100 * 1024 * 1024  # zip-bomb guard
MAX_DOCX_BODY_BYTES = 40 * 1024 * 1024  # word/document.xml: python-docx parses all of it at once
MAX_CHUNKS_PER_FILE = 25000  # a safety cap against hostile uploads (a long book is ~3000)
# What is read of ONE document before it is refused, so that a hostile or huge upload cannot
# exhaust the gateway's memory: the cap on the text kept (about what MAX_CHUNKS_PER_FILE chunks
# hold), the size of a plain-text file, and the page count of a PDF.
MAX_DOCUMENT_CHARS = MAX_CHUNKS_PER_FILE * (CHUNK_CHARS - CHUNK_OVERLAP)
MAX_TEXT_FILE_BYTES = 25 * 1024 * 1024  # .txt and .md
MAX_JSON_FILE_BYTES = 10 * 1024 * 1024  # .json is pretty-printed, which makes it larger again
MAX_PDF_PAGES = 5000
ADD_BATCH = 64  # chunks embedded and stored per step; also the cancel granularity
STORE_BATCH = 500  # ids per metadata update / delete call
GET_PAGE = 2000  # rows per read: ChromaDB fails on a very large one (too many SQL variables)
LEGACY_COLLECTION = "documents"  # the collection of vector space ``minilm-l6-v2``
MAX_COLLECTION_CHARS = 63
PENDING = "pending"  # sha of a chunk of a file that is not completely indexed yet
FAILED_MESSAGE = "unreadable or unsupported document"
NO_TEXT_MESSAGE = "no readable text (a scanned document needs OCR)"
MIN_CHARS_PER_PAGE = 80  # below this average a PDF is treated as scanned / image-only
OCR_TIMEOUT = 600.0  # seconds for the whole OCR run (rasterise + every page)
OCR_MAX_PAGES = 400
OCR_SCALE_TO = 2000  # pdftoppm -scale-to: longest page side in pixels, whatever the page size
OCR_MAX_TEMP_BYTES = 300 * 1024 * 1024  # rasterised pages kept per OCR run
OCR_SLOT_WAIT = 5.0  # seconds a second upload waits for the one OCR slot before skipping OCR
OCR_TOOLS = ("pdftoppm", "tesseract")


_ocr_slots = threading.BoundedSemaphore(1)  # one OCR run at a time: it is CPU and disk heavy


class Embedder(Protocol):
    """What the index needs from an embedder (see :class:`pwndoc_ai.embedder.OllamaEmbedder`)."""

    name: str
    space: str  # id of the vector space: vectors of one space can share a collection

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    def close(self) -> None: ...


EmbedFn = Callable[[Sequence[str]], list[list[float]]]


def safe_filename(name: str) -> str:
    cleaned = SAFE_NAME.sub("_", Path(name).name).strip(". ")
    if len(cleaned) > MAX_NAME_CHARS:
        suffix = Path(cleaned).suffix[:16]
        cleaned = cleaned[: MAX_NAME_CHARS - len(suffix)].rstrip(". ") + suffix
    return cleaned or "document"


def iter_chunks(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> Iterator[str]:
    """The chunks of ``text`` one at a time, so a caller can stop as soon as there are too many."""
    text = re.sub(r"[ \t]+", " ", text).strip()
    step = max(1, size - overlap)
    for start in range(0, len(text), step):
        yield text[start : start + size]


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    return list(iter_chunks(text, size, overlap))


def max_file_bytes(suffix: str) -> int:
    """The largest file of a type that is accepted (types of any size have the upload cap)."""
    if suffix.lower() in (".txt", ".md"):
        return MAX_TEXT_FILE_BYTES
    if suffix.lower() == ".json":
        return MAX_JSON_FILE_BYTES
    return 2**63 - 1


def _too_large() -> DocumentError:
    return DocumentError("document is too large to index")


def _read_text_bounded(path: Path, limit: int) -> str:
    """The text of a file; ``DocumentError`` if it is (or has grown) larger than ``limit`` bytes."""
    if path.stat().st_size > limit:
        raise _too_large()
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise _too_large()
    return raw.decode("utf-8", errors="replace")


class _TextBudget:
    """Counts the characters collected from a document and refuses it past the cap."""

    def __init__(self, limit: int | None = None):
        self._limit = MAX_DOCUMENT_CHARS if limit is None else limit
        self._used = 0

    def add(self, text: str) -> str:
        self._used += len(text)
        if self._used > self._limit:
            raise _too_large()
        return text


def _check_docx_size(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            expanded = sum(info.file_size for info in archive.infolist())
            body = max(
                (i.file_size for i in archive.infolist() if i.filename.endswith(".xml")), default=0
            )
    except zipfile.BadZipFile as exc:
        raise DocumentError("not a valid .docx file") from exc
    if expanded > MAX_DOCX_UNCOMPRESSED:
        raise DocumentError("document expands to more than 100 MB")
    if body > MAX_DOCX_BODY_BYTES:
        raise _too_large()


def read_pdf(path: Path) -> tuple[str, int]:
    """Extracted text and page count of a PDF."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RagUnavailable("pypdf is not installed (pip install 'pwndoc-ai[rag]')") from exc
    pages = PdfReader(str(path)).pages
    count = len(pages)
    if count > MAX_PDF_PAGES:
        raise DocumentError(f"document has more than {MAX_PDF_PAGES} pages")
    budget = _TextBudget(MAX_DOCUMENT_CHARS)  # the text is checked page by page, as it is read
    return "\n".join(budget.add(page.extract_text() or "") for page in pages), count


def is_low_text(text: str, pages: int) -> bool:
    """True when the average non-whitespace characters per page is under the threshold."""
    if pages <= 0:
        return True
    return len(re.sub(r"\s+", "", text)) / pages < MIN_CHARS_PER_PAGE


def _rasterise(
    pdf: Path, workdir: Path, pdftoppm: str, max_pages: int, budget: float, run: Callable[..., Any]
) -> list[Path]:
    argv = [
        pdftoppm,
        "-scale-to",
        str(OCR_SCALE_TO),
        "-png",
        "-l",
        str(max_pages),
        str(pdf),
        str(workdir / "page"),
    ]
    try:
        done = run(  # noqa: S603 - argv list
            argv,
            capture_output=True,
            timeout=budget,
            check=False,
            watch_dir=workdir,
            max_dir_bytes=OCR_MAX_TEMP_BYTES,
        )
    except DiskBudgetExceeded:
        log.warning("pdftoppm was stopped: its page images outgrew the size budget")
        written = sorted(workdir.glob("page-*.png"))[:-1]  # the page it was writing is cut short
        return _within_budget(written[:max_pages])
    if done.returncode != 0:
        log.warning("pdftoppm failed for %s (exit %s)", pdf.name, done.returncode)
        return []
    return _within_budget(sorted(workdir.glob("page-*.png"))[:max_pages])


def _within_budget(images: list[Path]) -> list[Path]:
    """The leading pages whose combined size fits ``OCR_MAX_TEMP_BYTES``."""
    kept: list[Path] = []
    total = 0
    for image in images:
        total += image.stat().st_size
        if total > OCR_MAX_TEMP_BYTES:
            log.warning("OCR page images exceed the size budget; using the first %d", len(kept))
            break
        kept.append(image)
    return kept


def _recognise(
    images: list[Path], tesseract: str, deadline: float, run: Callable[..., Any]
) -> list[str]:
    parts: list[str] = []
    for image in images:
        budget = deadline - time.monotonic()
        if budget <= 0:
            log.warning("OCR time budget exhausted after %d pages", len(parts))
            break
        try:
            result = run(  # noqa: S603 - fixed tool path from which(), argv list, no shell
                [tesseract, str(image), "stdout"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=budget,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.warning("tesseract timed out after %d pages", len(parts))
            break
        if result.returncode == 0:
            parts.append(result.stdout)
    return parts


def ocr_available(which: Callable[[str], str | None] = shutil.which) -> bool:
    """Whether both OCR tools are installed."""
    return all(which(name) for name in OCR_TOOLS)


def ocr_text(
    path: Path,
    timeout: float = OCR_TIMEOUT,
    max_pages: int = OCR_MAX_PAGES,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = run_limited,
) -> str:
    """OCR ``path`` with pdftoppm + tesseract; ``""`` unless both are installed.

    One shared time budget covers rasterising and every page; text gathered before a timeout is
    returned. The temp directory is always removed.
    """
    pdftoppm, tesseract = (which(name) for name in OCR_TOOLS)
    if not pdftoppm or not tesseract or timeout <= 0:
        return ""
    if not _ocr_slots.acquire(timeout=OCR_SLOT_WAIT):
        log.warning("another document is being OCR'd; skipping OCR for %s", Path(path).name)
        return ""
    try:
        return _ocr_in_tempdir(Path(path), pdftoppm, tesseract, timeout, max_pages, run)
    finally:
        _ocr_slots.release()


def _ocr_in_tempdir(
    path: Path,
    pdftoppm: str,
    tesseract: str,
    timeout: float,
    max_pages: int,
    run: Callable[..., Any],
) -> str:
    deadline = time.monotonic() + timeout
    pdf = path.resolve()  # absolute, so it can never be parsed as an option
    with tempfile.TemporaryDirectory(prefix="pwndoc-ocr-") as tmp:
        try:
            images = _rasterise(pdf, Path(tmp), pdftoppm, max_pages, timeout, run)
            return "\n".join(_recognise(images, tesseract, deadline, run))
        except subprocess.TimeoutExpired:
            log.warning("pdftoppm timed out for %s", pdf.name)
        except OSError as exc:
            log.warning("OCR failed for %s: %s", pdf.name, exc.strerror or type(exc).__name__)
    return ""


def read_document(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return read_pdf(path)[0]
    if suffix == ".docx":
        _check_docx_size(path)  # before parsing: a zip bomb must never reach python-docx
        try:
            import docx
        except ImportError as exc:
            raise RagUnavailable(
                "python-docx is not installed (pip install 'pwndoc-ai[rag]')"
            ) from exc
        budget = _TextBudget()
        return "\n".join(budget.add(p.text) for p in docx.Document(str(path)).paragraphs)
    raw = _read_text_bounded(path, max_file_bytes(suffix))
    if suffix == ".json":
        try:
            return json.dumps(json.loads(raw), indent=1, ensure_ascii=False)
        except (ValueError, RecursionError):  # not JSON, or nested beyond what the parser takes
            return raw
    return raw


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")


def collection_name(embedder: Embedder | None) -> str:
    """ChromaDB collection of an embedder's vector space.

    The MiniLM space (ChromaDB's ONNX default and Ollama's ``all-minilm``) is the legacy
    collection ``documents``, so an index built with one embedder is reused by the other without
    embedding anything again. Any other space gets ``documents-<space>``.
    """
    space = MINILM_SPACE if embedder is None else embedder.space
    if space == MINILM_SPACE:
        return LEGACY_COLLECTION
    name = f"documents-{_slug(space) or 'space'}"
    if len(name) > MAX_COLLECTION_CHARS:
        digest = hashlib.sha256(space.encode()).hexdigest()[:8]
        name = f"{name[: MAX_COLLECTION_CHARS - 9]}-{digest}"
    return name.rstrip("-_.")


def chunk_id(source: str, text: str) -> str:
    """Id of a chunk: its document and a hash of its text (the same text is never stored twice)."""
    return f"{source}:{hashlib.sha256(text.encode()).hexdigest()[:32]}"


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _records(name: str, text: str) -> list[tuple[str, str]]:
    """``(id, text)`` of every distinct chunk of ``text`` in document order."""
    seen: set[str] = set()
    records: list[tuple[str, str]] = []
    for count, chunk in enumerate(iter_chunks(text), 1):
        if count > MAX_CHUNKS_PER_FILE:  # stop at once: the rest is never even cut
            raise DocumentError(f"document is too large (more than {MAX_CHUNKS_PER_FILE} chunks)")
        cid = chunk_id(name, chunk)
        if cid not in seen:
            seen.add(cid)
            records.append((cid, chunk))
    return records


@dataclass(frozen=True)
class _Stored:
    """What the index holds for one document name."""

    ids: frozenset[str] = frozenset()
    shas: frozenset[str] = frozenset()
    low_text: bool = False  # the stored text is (mostly) missing: a scan
    ocr_tried: bool = False  # OCR was available when it was indexed

    def complete_for(self, digest: str) -> bool:
        """Every stored chunk belongs to the file with this hash (nothing half done, no stale)."""
        return bool(self.ids) and self.shas == frozenset({digest})


@dataclass(frozen=True)
class _Extracted:
    text: str
    low_text: bool = False
    ocr_tried: bool = False


class DocumentIndex:
    def __init__(
        self, base_dir: str | Path, collection: Any | None = None, embedder: Embedder | None = None
    ):
        """``collection`` lets tests inject a fake; normally ChromaDB provides it.

        With an ``embedder`` the vectors are computed here and handed to ChromaDB; without one
        ChromaDB's built-in ONNX embedder does it. Opening an index embeds nothing.
        """
        self.base = Path(base_dir)
        self.docs_dir = self.base / "docs"
        self.docs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.embedder = embedder
        self._outcomes = OutcomeStore(self.base)
        self._sync_lock = threading.Lock()
        self._write_lock = threading.Lock()  # one file is indexed (or removed) at a time
        self._listing: tuple[int, tuple[tuple[str, int, bool], ...]] | None = None
        self._collection = collection if collection is not None else self._open_collection()

    def _open_collection(self) -> Any:
        try:
            import chromadb
        except ImportError as exc:
            raise RagUnavailable(
                "chromadb is not installed (pip install 'pwndoc-ai[rag]')"
            ) from exc
        client = chromadb.PersistentClient(path=str(self.base / "chroma"))
        name = collection_name(self.embedder)
        if self.embedder is None:
            return client.get_or_create_collection(name)
        # vectors come from us (also for a collection ChromaDB created with its own embedder)
        return client.get_or_create_collection(name, embedding_function=None)

    @property
    def embedder_name(self) -> str:
        return self.embedder.name if self.embedder is not None else "onnx:all-MiniLM-L6-v2"

    @property
    def space(self) -> str:
        return self.embedder.space if self.embedder is not None else MINILM_SPACE

    def close(self) -> None:
        """Release what the index holds (stops an on-demand embedder server)."""
        if self.embedder is not None:
            self.embedder.close()

    def _vectors(self, texts: list[str], embed: EmbedFn | None = None) -> list[list[float]] | None:
        """Embeddings for ``texts`` from our embedder, or ``None`` when ChromaDB embeds.

        ``embed`` replaces the embedder's own ``embed`` (a guarded one with a deadline, of the
        same vector space) and also makes the vectors come from us when ChromaDB would embed.
        """
        if embed is None and self.embedder is None:
            return None
        vectors = embed(texts) if embed is not None else self.embedder.embed(texts)
        if len(vectors) != len(texts):
            raise EmbedderError("the embedder returned the wrong number of vectors")
        if len({len(vector) for vector in vectors}) > 1:
            raise EmbedderError("the embedder returned vectors of different sizes")
        return vectors

    # -- indexing -------------------------------------------------------------------------
    def add_file(self, path: Path) -> int:
        """Index ``path``; the number of chunks it now has, or 0 if nothing was indexed.

        Raises for an unreadable document; :meth:`index_file` reports it instead.
        """
        result = self._index(Path(path), None)
        return result.chunks_new + result.chunks_reused if result.status == "indexed" else 0

    def index_file(self, path: Path, should_stop: Callable[[], bool] | None = None) -> FileResult:
        """Index ``path`` incrementally and say what happened.

        A document that cannot be read is a ``failed`` result (the stored chunks are untouched);
        an unavailable embedder (``EmbedderError``) and a cancel (``IndexCancelled``, polled
        through ``should_stop`` before each batch) are raised.
        """
        path = Path(path)
        try:
            return self._index(path, should_stop)
        except (EmbedderError, IndexCancelled):
            raise
        except DocumentError as exc:
            log.warning("could not index %s: %s", path.name, exc)
            return FileResult(path.name, "failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - one bad document must not stop the others
            log.warning("could not index %s (%s)", path.name, type(exc).__name__)
            return FileResult(path.name, "failed", error=FAILED_MESSAGE)

    def _index(self, path: Path, should_stop: Callable[[], bool] | None) -> FileResult:
        if path.suffix.lower() not in SUPPORTED:
            raise DocumentError(f"unsupported file type: {path.suffix}")
        if path.is_symlink() or not path.is_file():
            raise DocumentError("not a regular file")
        with self._write_lock:
            try:
                return self._index_locked(path, should_stop)
            finally:
                self._listing = None  # whatever happened, the stored rows may have changed

    def _index_locked(self, path: Path, should_stop: Callable[[], bool] | None) -> FileResult:
        name = path.name
        digest = _file_sha(path)
        stored = self._stored(name)
        early = self._early_result(path, digest, stored)
        if early is not None:
            return early
        try:
            extracted = self._extract(path)  # parse first: a bad file keeps the old chunks
            records = _records(name, extracted.text)
        except DocumentError as exc:
            self._remember(path, digest, "failed", str(exc))  # the same bytes fail the same way
            raise
        if not records:
            self._remember(path, digest, "failed", NO_TEXT_MESSAGE, ocr=extracted.ocr_tried)
            return FileResult(name, "failed", error=NO_TEXT_MESSAGE)
        result = self._write(name, digest, records, stored, extracted, should_stop)
        self._outcomes.clear(name)
        return result

    def _early_result(self, path: Path, digest: str, stored: _Stored) -> FileResult | None:
        """``skipped``, ``duplicate`` or a known ``failed`` when there is nothing to do."""
        name = path.name
        complete = stored.complete_for(digest)
        if complete and not _worth_a_retry(stored):
            self._outcomes.clear(name)
            return FileResult(name, "skipped")
        if not complete and self._has_other_copy(name, digest):
            self._delete_ids(sorted(stored.ids))  # the other copy already covers this content
            self._remember(path, digest, "duplicate", "")
            return FileResult(name, "duplicate")
        known = self._outcomes.get(name, digest)
        if known is not None and known.status == "failed" and not _ocr_now_possible(known):
            return FileResult(name, "failed", error=known.error)  # not parsed again: same bytes
        return None

    def _remember(
        self, path: Path, digest: str, status: str, error: str, *, ocr: bool = False
    ) -> None:
        """Note why ``path`` has no chunks, for as long as it keeps these bytes."""
        try:
            info = path.stat()
        except OSError:
            return
        self._outcomes.record(
            path.name, Outcome(status, error, digest, info.st_size, info.st_mtime_ns, ocr)
        )

    def outcomes(self) -> dict[str, dict[str, str]]:
        """``{name: {status, error}}`` of stored files that are known not to be indexable.

        ``status`` is ``duplicate`` (the content is indexed under another name) or ``failed``;
        only files that still have the bytes the note was made for are listed.
        """
        current = self._outcomes.current(self.docs_dir)
        return {
            name: {"status": o.status, "error": o.error}
            for name, o in current.items()
            # a duplicate counts only while the other copy it duplicates is still indexed
            if o.status != "duplicate" or self._has_other_copy(name, o.sha)
        }

    def _write(
        self,
        name: str,
        digest: str,
        records: list[tuple[str, str]],
        stored: _Stored,
        extracted: _Extracted,
        should_stop: Callable[[], bool] | None,
    ) -> FileResult:
        todo = [(cid, text) for cid, text in records if cid not in stored.ids]
        flags = {"low_text": extracted.low_text, "ocr": extracted.ocr_tried}
        for start in range(0, len(todo), ADD_BATCH):
            if should_stop is not None and should_stop():
                raise IndexCancelled("indexing was cancelled")
            self._add_batch(name, todo[start : start + ADD_BATCH], flags)
        keep = [cid for cid, _ in records]
        self._delete_ids(sorted(stored.ids - set(keep)))  # only now that every new chunk is in
        self._mark_complete(name, digest, keep, flags)
        return FileResult(
            name, "indexed", chunks_new=len(todo), chunks_reused=len(records) - len(todo)
        )

    def _add_batch(self, name: str, batch: list[tuple[str, str]], flags: dict[str, bool]) -> None:
        texts = [text for _, text in batch]
        vectors = self._vectors(texts)
        extra = {} if vectors is None else {"embeddings": vectors}
        self._collection.add(
            ids=[cid for cid, _ in batch],
            documents=texts,
            metadatas=[{"source": name, "sha": PENDING, **flags} for _ in batch],
            **extra,
        )

    def _mark_complete(
        self, name: str, digest: str, ids: list[str], flags: dict[str, bool]
    ) -> None:
        """Stamp every kept chunk with the file hash: the commit point of an indexing run."""
        for start in range(0, len(ids), STORE_BATCH):
            batch = ids[start : start + STORE_BATCH]
            meta = {"source": name, "sha": digest, **flags}
            self._collection.update(ids=batch, metadatas=[dict(meta) for _ in batch])

    def _delete_ids(self, ids: list[str]) -> None:
        for start in range(0, len(ids), STORE_BATCH):
            self._collection.delete(ids=ids[start : start + STORE_BATCH])

    def _metadatas(
        self, where: dict[str, Any] | None = None
    ) -> Iterator[tuple[list[str], list[Any]]]:
        """``(ids, metadatas)`` of the matching rows, one page at a time."""
        offset = 0
        while True:
            query: dict[str, Any] = {"include": ["metadatas"], "limit": GET_PAGE, "offset": offset}
            if where is not None:
                query["where"] = where
            page = self._collection.get(**query)
            ids = list(page["ids"])
            if not ids:
                return
            yield ids, list(page["metadatas"] or [])
            if len(ids) < GET_PAGE:
                return
            offset += len(ids)

    def _stored(self, name: str) -> _Stored:
        ids: set[str] = set()
        shas: set[str] = set()
        low_text = ocr_tried = False
        for page_ids, metas in self._metadatas({"source": name}):
            ids.update(page_ids)
            shas.update(str(meta.get("sha", "")) for meta in metas)
            low_text = low_text or any(meta.get("low_text") for meta in metas)
            ocr_tried = ocr_tried or any(meta.get("ocr") for meta in metas)
        return _Stored(frozenset(ids), frozenset(shas), low_text, ocr_tried)

    def _has_other_copy(self, name: str, digest: str) -> bool:
        """Whether another, completely indexed document has exactly these bytes."""
        found = self._collection.get(
            where={"$and": [{"sha": digest}, {"source": {"$ne": name}}]},
            include=["metadatas"],
            limit=1,
        )
        for meta in found["metadatas"] or []:
            other = meta.get("source")
            if isinstance(other, str) and self._stored(other).complete_for(digest):
                return True
        return False

    def sync(self) -> SyncResult:
        """Index every supported file in the documents folder that is new or changed.

        Files whose content is already indexed are skipped by hash (nothing is parsed or
        embedded for them), so this is cheap to run again and after switching embedder. One bad
        file does not stop the rest; a broken embedder does (it would fail every file).
        """
        if not self._sync_lock.acquire(blocking=False):
            raise IndexBusy("indexing is already running")
        try:
            return self._sync_files()
        finally:
            self._sync_lock.release()

    def _sync_files(self) -> SyncResult:
        indexed: list[tuple[str, int]] = []
        unchanged: list[str] = []
        failed: list[tuple[str, str]] = []
        for path in sorted(self.docs_dir.iterdir()):
            if path.suffix.lower() not in SUPPORTED or path.is_symlink() or not path.is_file():
                continue
            result = self.index_file(path)
            if result.status == "indexed":
                indexed.append((path.name, result.chunks_new + result.chunks_reused))
            elif result.status == "failed":
                failed.append((path.name, result.error))
            else:
                unchanged.append(path.name)
        return SyncResult(tuple(indexed), tuple(unchanged), tuple(failed))

    @staticmethod
    def _extract(path: Path) -> _Extracted:
        """Document text, whether it is a (mostly) image-only PDF and whether OCR could run."""
        if path.suffix.lower() != ".pdf":
            return _Extracted(read_document(path))
        text, pages = read_pdf(path)
        if not is_low_text(text, pages):
            return _Extracted(text)
        tried = ocr_available()
        try:
            ocr = ocr_text(path)
        except Exception as exc:  # noqa: BLE001 - OCR is best-effort, the PDF stays indexed
            log.warning("OCR fallback failed for %s (%s)", path.name, type(exc).__name__)
            ocr = ""
        if len(ocr.strip()) > len(text.strip()):
            text = ocr
        return _Extracted(text, is_low_text(text, pages), tried)

    def remove(self, name: str) -> None:
        """Forget ``name``: drop its chunks and delete the stored file."""
        with self._write_lock:
            try:
                self._collection.delete(where={"source": name})
            finally:
                self._listing = None
            (self.docs_dir / name).unlink(missing_ok=True)
            self._outcomes.clear(name)

    # -- searching ------------------------------------------------------------------------
    def list_documents(self) -> list[dict[str, object]]:
        """``[{name, chunks, low_text}]``; cached until this index writes or the row count moves."""
        total = self._collection.count()
        cached = self._listing
        if cached is None or cached[0] != total:
            cached = (total, self._scan_documents())
            self._listing = cached
        return [{"name": n, "chunks": c, "low_text": low} for n, c, low in cached[1]]

    def _scan_documents(self) -> tuple[tuple[str, int, bool], ...]:
        counts: dict[str, int] = {}
        low: set[str] = set()
        for _, metas in self._metadatas():
            for meta in metas:
                counts[meta["source"]] = counts.get(meta["source"], 0) + 1
                if meta.get("low_text"):
                    low.add(meta["source"])
        return tuple((name, counts[name], name in low) for name in sorted(counts))

    def query_chunks(self, text: str, k: int = 5, embed: EmbedFn | None = None) -> list[Chunk]:
        """The ``k`` passages closest to ``text``; ``embed`` embeds the query (see ``_vectors``)."""
        if not text.strip() or self._collection.count() == 0:
            return []
        vectors = self._vectors([text], embed)
        probe = {"query_texts": [text]} if vectors is None else {"query_embeddings": vectors}
        result = self._collection.query(n_results=k, **probe)
        return [
            Chunk(source=meta["source"], text=doc[:MAX_CONTEXT_CHARS])
            for doc, meta in zip(result["documents"][0], result["metadatas"][0], strict=True)
        ]

    def query(self, text: str, k: int = 5) -> str:
        return format_chunks(self.query_chunks(text, k))


def _ocr_now_possible(known: Outcome) -> bool:
    """A scan that failed before OCR was installed is read again once OCR is available."""
    return not known.ocr and known.error == NO_TEXT_MESSAGE and ocr_available()


def _worth_a_retry(stored: _Stored) -> bool:
    """A scanned PDF is read again only when OCR has become available since it was indexed."""
    return stored.low_text and not stored.ocr_tried and ocr_available()
