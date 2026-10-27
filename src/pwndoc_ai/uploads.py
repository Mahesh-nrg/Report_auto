"""Storing uploaded documents (PDFs, books, notes) in the documents folder. Store only.

An upload is streamed to a temporary file next to its final place, so a half-received file is never
visible as a document and the move into place is one atomic rename. Nothing here trusts the client:

* the name is reduced to safe characters and its type must be one the index can read;
* the size cap and the folder quota count the bytes actually received, never ``Content-Length``;
* the first bytes must match the type (``%PDF``, a zip for ``.docx``) and text types may not hold
  NUL bytes;
* a different file under an existing name is refused; identical bytes already stored (under this or
  another name) are reported and not stored a second time. A name is claimed with a hard link (or an
  exclusive create), which fails when the name is taken: nothing is ever replaced, whichever store
  object, worker or tool writes to the folder at the same time.

Uploading never indexes: indexing is a separate, explicit run (see :mod:`pwndoc_ai.indexer`).
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import tempfile
import threading
import time
import zipfile
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from . import rag
from .rag import MAX_DOCX_UNCOMPRESSED, SUPPORTED, safe_filename

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 300 * 1024 * 1024  # one book; the hard cap per file
DOCS_QUOTA_BYTES = 5 * 1024**3  # everything stored in the documents folder
MULTIPART_OVERHEAD = 1024 * 1024  # boundaries and headers around the one file
MAX_DOCX_EXPANDED = MAX_DOCX_UNCOMPRESSED  # zip-bomb guard, same limit the indexer applies
TEMP_PREFIX = ".upload-"
TEMP_SUFFIX = ".part"  # not a supported type: an index run never picks it up
STALE_TEMP_SECONDS = 3600.0  # a temp file untouched this long belongs to a crashed upload
HASH_BLOCK = 1024 * 1024
MAGIC = {".pdf": b"%PDF", ".docx": b"PK\x03\x04"}
MAGIC_BYTES = 4
TEXT_SUFFIXES = frozenset({".txt", ".md", ".json"})
STORED, EXISTS, DUPLICATE = "stored", "exists", "duplicate"

# errors of a hard link that mean "this file system cannot do that", not "the name is taken"
NO_HARD_LINKS = frozenset(
    {errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV, errno.EMLINK, errno.EACCES}
)
_FOLDER_LOCKS: dict[Path, threading.Lock] = {}
_FOLDER_LOCKS_GUARD = threading.Lock()

HTTP_BAD_REQUEST, HTTP_TIMEOUT, HTTP_CONFLICT, HTTP_TOO_LARGE = 400, 408, 409, 413
HTTP_UNSUPPORTED, HTTP_STORAGE_FULL = 415, 507


class UploadError(Exception):
    """A refused or failed upload; ``message`` is safe to show (no paths, no internals)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class StoredDocument:
    """The outcome of one upload."""

    name: str
    size: int
    status: str  # stored | exists | duplicate
    duplicate_of: str = ""

    def as_dict(self) -> dict[str, object]:
        body: dict[str, object] = {"name": self.name, "size": self.size, "status": self.status}
        if self.duplicate_of:
            body["duplicate_of"] = self.duplicate_of
        return body


@dataclass(frozen=True)
class StoredFile:
    name: str
    size: int


def upload_body_limit() -> int:
    """Largest request body the upload route accepts: the file cap plus multipart framing."""
    return MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD


def lock_for(docs_dir: str | Path) -> threading.Lock:
    """The one lock of a documents folder, whichever store object (or index reopening) asks."""
    key = Path(docs_dir).resolve()
    with _FOLDER_LOCKS_GUARD:
        return _FOLDER_LOCKS.setdefault(key, threading.Lock())


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.0f} MB" if size >= 1024 * 1024 else f"{size} bytes"


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(HASH_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def _clean_name(filename: str) -> str:
    """The stored name for an uploaded file name (400 / 415 when it cannot be accepted)."""
    if not filename or any(ord(c) < 32 or ord(c) == 127 for c in filename):
        raise UploadError(HTTP_BAD_REQUEST, "the file name is not valid")
    name = safe_filename(filename)
    if Path(name).suffix.lower() not in SUPPORTED:
        raise UploadError(
            HTTP_UNSUPPORTED, f"unsupported file type; use one of {sorted(SUPPORTED)}"
        )
    return name


def _check_docx(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            expanded = sum(info.file_size for info in archive.infolist())
    except (zipfile.BadZipFile, OSError) as exc:
        raise UploadError(HTTP_BAD_REQUEST, "the file is not a valid .docx document") from exc
    if expanded > MAX_DOCX_EXPANDED:
        raise UploadError(HTTP_BAD_REQUEST, "the document expands to more than 100 MB")


class DocumentStore:
    """The documents folder: where uploads land, and the lock that keeps names and bytes unique."""

    def __init__(
        self, docs_dir: str | Path, *, max_bytes: int | None = None, quota_bytes: int | None = None
    ):
        self.docs_dir = Path(docs_dir)
        self.max_bytes = MAX_UPLOAD_BYTES if max_bytes is None else max_bytes
        self.quota_bytes = DOCS_QUOTA_BYTES if quota_bytes is None else quota_bytes
        self.lock = lock_for(self.docs_dir)  # held while a finished upload is moved into place

    # -- reading --------------------------------------------------------------------------
    def list_files(self) -> list[StoredFile]:
        """Every stored document (supported type, regular file, no temp files), by name."""
        return sorted(self._stored(), key=lambda f: f.name)

    def stored_bytes(self) -> int:
        return sum(entry.size for entry in self._stored())

    def _stored(self) -> Iterator[StoredFile]:
        try:
            entries = list(self.docs_dir.iterdir())
        except OSError:
            return
        for path in entries:
            if path.name.startswith(".") or path.suffix.lower() not in SUPPORTED:
                continue
            with suppress(OSError):  # a file removed while listing is simply not listed
                if path.is_file() and not path.is_symlink():
                    yield StoredFile(path.name, path.stat().st_size)

    # -- writing --------------------------------------------------------------------------
    def begin(self, filename: str) -> UploadSink:
        """Validate the name and open a temporary file for the incoming bytes."""
        name = _clean_name(filename)
        stored = self.stored_bytes()
        if stored >= self.quota_bytes:
            raise UploadError(HTTP_STORAGE_FULL, "the documents folder is full")
        try:
            self.docs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._remove_stale_temp_files()
            fd, temp = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX, dir=self.docs_dir)
        except OSError as exc:
            log.warning("cannot create an upload file: %s", exc.strerror or type(exc).__name__)
            raise UploadError(HTTP_STORAGE_FULL, "the file could not be stored") from exc
        return UploadSink(self, name, os.fdopen(fd, "wb"), Path(temp), stored)

    def _remove_stale_temp_files(self) -> None:
        cutoff = time.time() - STALE_TEMP_SECONDS
        for path in self.docs_dir.glob(f"{TEMP_PREFIX}*{TEMP_SUFFIX}"):
            with suppress(OSError):
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()

    def commit(self, name: str, temp: Path, size: int, digest: str) -> StoredDocument:
        """Move a finished upload into place, or report why it was not (atomic, one at a time)."""
        target = self.docs_dir / name
        with self.lock:
            if self._target_taken(target):
                return self._existing(target, name, size, digest)
            twin = self._same_bytes(name, size, digest)
            if twin:
                return StoredDocument(name, size, DUPLICATE, duplicate_of=twin)
            if self.stored_bytes() + size > self.quota_bytes:
                raise UploadError(HTTP_STORAGE_FULL, "the documents folder is full")
            if not self._claim(temp, target):  # a writer outside this lock got there first
                return self._existing(target, name, size, digest)
        return StoredDocument(name, size, STORED)

    @staticmethod
    def _target_taken(target: Path) -> bool:
        return target.exists() or target.is_symlink()

    @staticmethod
    def _claim(temp: Path, target: Path) -> bool:
        """Give the finished file its name; ``False`` when the name is taken (never replaces).

        A hard link fails if the name exists (a dangling symlink included), unlike a rename. A
        file system without hard links gets an exclusive create and a copy instead.
        """
        try:
            os.link(temp, target)
        except FileExistsError:
            return False
        except OSError as exc:
            if exc.errno not in NO_HARD_LINKS:
                log.warning("cannot move an upload into place: %s", exc.strerror or "I/O error")
                raise UploadError(HTTP_STORAGE_FULL, "the file could not be stored") from exc
            return DocumentStore._copy_exclusive(temp, target)
        with suppress(OSError):
            temp.unlink()
        return True

    @staticmethod
    def _copy_exclusive(temp: Path, target: Path) -> bool:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(target, flags, 0o600)
        except FileExistsError:
            return False
        except OSError as exc:
            log.warning("cannot move an upload into place: %s", exc.strerror or "I/O error")
            raise UploadError(HTTP_STORAGE_FULL, "the file could not be stored") from exc
        try:
            with os.fdopen(fd, "wb") as out, temp.open("rb") as source:
                while block := source.read(HASH_BLOCK):
                    out.write(block)
        except OSError as exc:
            target.unlink(missing_ok=True)  # never leave half a document under a real name
            log.warning("cannot move an upload into place: %s", exc.strerror or "I/O error")
            raise UploadError(HTTP_STORAGE_FULL, "the file could not be stored") from exc
        with suppress(OSError):
            temp.unlink()
        return True

    @staticmethod
    def _existing(target: Path, name: str, size: int, digest: str) -> StoredDocument:
        same = (
            target.is_file()
            and not target.is_symlink()
            and target.stat().st_size == size
            and _file_sha(target) == digest
        )
        if not same:
            raise UploadError(
                HTTP_CONFLICT,
                "a different file with this name is already stored; delete it first or rename "
                "the upload",
            )
        return StoredDocument(name, size, EXISTS)

    def _same_bytes(self, name: str, size: int, digest: str) -> str:
        """The stored file with these exact bytes (only equal-sized files are hashed), or ''."""
        for entry in self._stored():
            if entry.size == size and entry.name != name:
                with suppress(OSError):
                    if _file_sha(self.docs_dir / entry.name) == digest:
                        return entry.name
        return ""


class UploadSink:
    """Receives one file's bytes in order, checking them as they arrive."""

    def __init__(
        self, store: DocumentStore, name: str, handle: BinaryIO, temp: Path, stored_before: int
    ):
        self._store = store
        self._name = name
        self._suffix = Path(name).suffix.lower()
        self._handle: BinaryIO | None = handle
        self._temp = temp
        self._stored_before = stored_before
        self._digest = hashlib.sha256()
        self._written = 0
        self._head = b""
        self._sniffed = False

    @property
    def name(self) -> str:
        return self._name

    def write(self, chunk: bytes) -> None:
        """Append received bytes; raises :class:`UploadError` as soon as one rule is broken."""
        if not chunk:
            return
        self._written += len(chunk)
        cap = min(self._store.max_bytes, rag.max_file_bytes(self._suffix))
        if self._written > cap:
            raise UploadError(HTTP_TOO_LARGE, f"file is larger than {_megabytes(cap)}")
        if self._stored_before + self._written > self._store.quota_bytes:
            raise UploadError(HTTP_STORAGE_FULL, "the documents folder is full")
        self._sniff(chunk)
        self._digest.update(chunk)
        self._append(chunk)

    def finish(self) -> StoredDocument:
        """Close the file, run the final checks and move it into place (cleans up on failure)."""
        try:
            self._close()
            if self._written == 0:
                raise UploadError(HTTP_BAD_REQUEST, "the file is empty")
            if not self._sniffed:
                self._check_magic()
            if self._suffix == ".docx":
                _check_docx(self._temp)
            return self._store.commit(
                self._name, self._temp, self._written, self._digest.hexdigest()
            )
        finally:
            self.abort()

    def abort(self) -> None:
        """Drop the temporary file (safe to call more than once, and after a commit)."""
        self._close()
        with suppress(OSError):
            self._temp.unlink()

    def _close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError as exc:
                log.warning("cannot finish an upload file: %s", exc.strerror or "I/O error")
                raise UploadError(HTTP_STORAGE_FULL, "the file could not be stored") from exc

    def _append(self, chunk: bytes) -> None:
        try:
            assert self._handle is not None  # noqa: S101 - write() is never called after finish()
            self._handle.write(chunk)
        except OSError as exc:  # disk full, quota, unplugged drive
            log.warning("cannot write an upload: %s", exc.strerror or type(exc).__name__)
            raise UploadError(HTTP_STORAGE_FULL, "the file could not be stored") from exc

    def _sniff(self, chunk: bytes) -> None:
        if self._suffix in TEXT_SUFFIXES and b"\x00" in chunk:
            raise UploadError(HTTP_BAD_REQUEST, "the file is not a text document")
        if not self._sniffed:
            self._head += chunk[: MAGIC_BYTES - len(self._head)]
            if len(self._head) >= MAGIC_BYTES:
                self._check_magic()

    def _check_magic(self) -> None:
        self._sniffed = True
        expected = MAGIC.get(self._suffix)
        if expected is not None and not self._head.startswith(expected):
            raise UploadError(
                HTTP_BAD_REQUEST, f"the content is not a valid {self._suffix[1:].upper()} file"
            )
