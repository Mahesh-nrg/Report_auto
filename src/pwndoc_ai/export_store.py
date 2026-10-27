"""Per-user storage of the Export tab: uploaded DOCX or PDF files and what the analysis produced.

Layout (docs/EXPORT_DESIGN.md (b)): ``<data_dir>/export/<owner_hash>/<id>/`` (directories 0700,
files 0600) holding ``meta.json`` (an :class:`ExportRecord`), ``source.docx`` or ``source.pdf``,
and after an analysis ``converted.docx`` (PDF only), ``report.json``, ``sections.json``,
``fonts.json`` and ``plan.json``. A file belongs to the user who uploaded it: for anybody else it
does not exist. Ids are uuid4 hex (``docx_store.FILE_ID``); anything else is "not found".

Uploads are streamed by ``upload_stream.receive_upload`` into a staging file; the suffix decides
the cap (50 MiB DOCX, 100 MiB PDF) and must agree with the magic bytes. Nothing is stored until
:meth:`ExportStore.commit`, which checks the quotas under the store lock.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from . import export_json as jc
from .docx_store import FILE_ID, owner_hash, precheck_docx
from .export_errors import ExportFailure, ExportLimitError, ExportNotFound, ExportUnsupported
from .uploads import UploadError, lock_for

log = logging.getLogger(__name__)

MAX_EXPORT_DOCX_BYTES = 50 * 1024 * 1024
MAX_EXPORT_PDF_BYTES = 100 * 1024 * 1024
USER_MAX_FILES = 10
USER_MAX_BYTES = 1024**3
GLOBAL_MAX_BYTES = 5 * 1024**3
RESERVE = 3  # source + converted copy + room for the analysis files
MAX_META_BYTES = 64 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024  # sections.json of a very long document
MAX_OWNER_ITEMS = 200
MAX_NAME = 120
STALE_TEMP_SECONDS = 3600.0
TEMP_NAME = ".tmp"
TEMP_PREFIX = ".upload-"
TEMP_SUFFIX = ".part"
MAGIC = {"docx": b"PK\x03\x04", "pdf": b"%PDF"}
MAGIC_BYTES = 4
SUFFIX_KIND = {".docx": "docx", ".pdf": "pdf"}
KIND_LIMITS = {"docx": MAX_EXPORT_DOCX_BYTES, "pdf": MAX_EXPORT_PDF_BYTES}
SOURCE_FILES = {"docx": "source.docx", "pdf": "source.pdf"}
DATA_FILES = frozenset(
    {"converted.docx", "report.json", "sections.json", "fonts.json", "plan.json"}
)
STATES = ("uploaded", "converting", "analysing", "ready", "error", "cancelled")
RUNNING_STATES = ("converting", "analysing")
ERROR_CODE = re.compile(r"(?:[A-Za-z]{1,40}\.[a-z_]{1,40}|interrupted)")
UNSAFE_NAME = re.compile(r"[^\w.\- ]+")
MAX_PLAN_REV = 1_000_000
MAX_PROGRESS = 10_000_000
INTERRUPTED = "interrupted"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _private_dirs(directory: Path) -> None:
    missing = [d for d in (directory, *directory.parents) if not d.exists()]
    for folder in reversed(missing):
        folder.mkdir(mode=0o700, exist_ok=True)


def atomic_write(target: Path, data: bytes) -> None:
    """Write via a unique temp file in the same directory, then replace (0600)."""
    descriptor, temp = tempfile.mkstemp(dir=target.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temp, target)
    except BaseException:
        with suppress(OSError):
            os.unlink(temp)
        raise


def clean_name(filename: str) -> tuple[str, str]:
    """The stored name and the kind (``docx``/``pdf``) of an uploaded file name."""
    if not filename or any(ord(c) < 32 or ord(c) == 127 for c in filename):
        raise UploadError(400, "the file name is not valid")
    base = UNSAFE_NAME.sub("_", Path(filename.replace("\\", "/")).name).strip(" .")
    suffix = Path(base).suffix.lower()
    kind = SUFFIX_KIND.get(suffix)
    if kind is None:
        raise UploadError(ExportUnsupported.status, str(ExportUnsupported()))
    stem = Path(base).stem[: MAX_NAME - len(suffix)] or "document"
    return stem + suffix, kind


# -- the record -------------------------------------------------------------------------------
@dataclass(frozen=True)
class ExportRecord:
    """``meta.json`` of one uploaded file (strict; see the module docstring)."""

    id: str
    name: str
    kind: str
    size: int
    created: str
    state: str = "uploaded"
    engine: str = "auto"
    error_code: str = ""
    done: int = 0
    total: int = 0
    plan_rev: int = 0

    @property
    def source_name(self) -> str:
        return SOURCE_FILES[self.kind]

    @property
    def running(self) -> bool:
        return self.state in RUNNING_STATES

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "size": self.size,
            "created": self.created,
            "state": self.state,
            "engine": self.engine,
            "error_code": self.error_code,
            "progress": {"done": self.done, "total": self.total},
            "plan_rev": self.plan_rev,
        }

    @classmethod
    def from_json(cls, value: Any) -> ExportRecord:
        keys = ("id", "name", "kind", "size", "created", "state", "engine", "error_code")
        data = jc.record(value, "meta", (*keys, "progress", "plan_rev"))
        progress = jc.record(data["progress"], "meta.progress", ("done", "total"))
        return cls(
            id=jc.required(data["id"], "meta.id", 32, FILE_ID),
            name=jc.required(data["name"], "meta.name", MAX_NAME, re.compile(r"[^/\\]+")),
            kind=jc.choice(data["kind"], "meta.kind", tuple(SOURCE_FILES)),
            size=jc.count(data["size"], "meta.size", MAX_EXPORT_PDF_BYTES),
            created=jc.text(data["created"], "meta.created", 40),
            state=jc.choice(data["state"], "meta.state", STATES),
            engine=jc.choice(data["engine"], "meta.engine", ("auto", "structure", "pdf2docx")),
            error_code=jc.text(data["error_code"], "meta.error_code", 82, pattern=ERROR_CODE),
            done=jc.count(progress["done"], "meta.progress.done", MAX_PROGRESS),
            total=jc.count(progress["total"], "meta.progress.total", MAX_PROGRESS),
            plan_rev=jc.count(data["plan_rev"], "meta.plan_rev", MAX_PLAN_REV),
        )


@dataclass(frozen=True)
class StagedExport:
    """A fully received upload that is not stored yet (commit or discard it)."""

    owner: str
    name: str
    kind: str
    size: int
    path: Path

    def discard(self) -> None:
        with suppress(OSError):
            self.path.unlink()


# -- the upload sink --------------------------------------------------------------------------
class ExportUploadSink:
    """Receives one file's bytes in order: cap by kind, magic bytes matching the suffix."""

    def __init__(self, owner: str, name: str, kind: str, handle: BinaryIO, temp: Path):
        self._owner, self._name, self._kind = owner, name, kind
        self._handle: BinaryIO | None = handle
        self._temp = temp
        self._written = 0
        self._head = b""

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._written += len(chunk)
        if self._written > KIND_LIMITS[self._kind]:
            raise UploadError(413, str(ExportLimitError("too_large")))
        if len(self._head) < MAGIC_BYTES:
            self._head += chunk[: MAGIC_BYTES - len(self._head)]
            if len(self._head) >= MAGIC_BYTES and self._head != MAGIC[self._kind]:
                raise UploadError(ExportUnsupported.status, str(ExportUnsupported()))
        try:
            if self._handle is None:
                raise OSError("closed")
            self._handle.write(chunk)
        except OSError as exc:
            log.warning("cannot write an export upload (%s)", type(exc).__name__)
            raise UploadError(507, "the file could not be stored") from exc

    def finish(self) -> StagedExport:
        try:
            self._close()
            if self._written == 0:
                raise UploadError(400, "the file is empty")
            if self._head != MAGIC[self._kind]:
                raise UploadError(ExportUnsupported.status, str(ExportUnsupported()))
            if self._kind == "docx":
                _docx_precheck(self._temp)
        except BaseException:
            self.abort()
            raise
        return StagedExport(self._owner, self._name, self._kind, self._written, self._temp)

    def abort(self) -> None:
        with suppress(UploadError):
            self._close()
        with suppress(OSError):
            self._temp.unlink()

    def _close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.close()
        except OSError as exc:
            raise UploadError(507, "the file could not be stored") from exc


def _docx_precheck(path: Path) -> None:
    """The DocxStore structural checks (zip bombs, macros, traversal) as a 422."""
    try:
        precheck_docx(path)
    except UploadError as exc:
        raise UploadError(422, exc.message) from exc


class OwnerExportUploads:
    """What ``receive_upload`` needs: ``begin(filename)`` -> a sink, for one user."""

    def __init__(self, store: ExportStore, owner: str):
        self._store, self._owner = store, owner

    def begin(self, filename: str) -> ExportUploadSink:
        return self._store.begin_upload(self._owner, filename)


# -- the store --------------------------------------------------------------------------------
class ExportStore:
    def __init__(
        self,
        root: str | Path,
        *,
        user_max_files: int = USER_MAX_FILES,
        user_max_bytes: int = USER_MAX_BYTES,
        global_max_bytes: int = GLOBAL_MAX_BYTES,
        clock: Callable[[], str] = _now,
    ):
        self.root = Path(root)
        self.user_max_files = user_max_files
        self.user_max_bytes = user_max_bytes
        self.global_max_bytes = global_max_bytes
        self._clock = clock
        self._lock = lock_for(self.root)
        self.recover()

    # -- paths ------------------------------------------------------------------------------
    @property
    def temp_dir(self) -> Path:
        return self.root / TEMP_NAME

    def work_dir(self) -> Path:
        """The private staging folder (also holds the PDF converter's scratch folders)."""
        _private_dirs(self.temp_dir)
        return self.temp_dir

    def _owner_dir(self, owner: str) -> Path:
        return self.root / owner_hash(owner)

    def folder(self, owner: str, file_id: str) -> Path:
        """The directory of ``file_id`` for ``owner``; :class:`ExportNotFound` when absent."""
        if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
            raise ExportNotFound()
        folder = self._owner_dir(owner) / file_id
        if folder.is_symlink() or not folder.is_dir():
            raise ExportNotFound()
        return folder

    def _iter_dirs(self, owner_dir: Path) -> Iterator[Path]:
        try:
            entries = sorted(owner_dir.iterdir())[:MAX_OWNER_ITEMS]
        except OSError:
            return
        for path in entries:
            if FILE_ID.fullmatch(path.name) and path.is_dir() and not path.is_symlink():
                yield path

    # -- records ----------------------------------------------------------------------------
    @staticmethod
    def _read_meta(folder: Path) -> ExportRecord:
        try:
            raw = _read_capped(folder / "meta.json", MAX_META_BYTES)
            record = ExportRecord.from_json(json.loads(raw))
        except (OSError, ValueError, RecursionError) as exc:
            raise ExportNotFound() from exc
        if record.id != folder.name:
            raise ExportNotFound()
        return record

    def get(self, owner: str, file_id: str) -> ExportRecord:
        return self._read_meta(self.folder(owner, file_id))

    def list(self, owner: str) -> list[ExportRecord]:
        records = []
        for folder in self._iter_dirs(self._owner_dir(owner)):
            with suppress(ExportNotFound):
                records.append(self._read_meta(folder))
        return sorted(records, key=lambda r: r.created, reverse=True)

    def update(self, owner: str, file_id: str, **changes: Any) -> ExportRecord:
        with self._lock:
            folder = self.folder(owner, file_id)
            record = replace(self._read_meta(folder), **changes)
            self._write_meta(folder, record)
            return record

    def bump_plan(self, owner: str, file_id: str, rev: int, plan: Mapping[str, Any]) -> int:
        """Store ``plan`` when ``rev`` is the current revision; the new revision, else -1."""
        with self._lock:
            folder = self.folder(owner, file_id)
            record = self._read_meta(folder)
            if record.plan_rev != rev:
                return -1
            new_rev = min(record.plan_rev + 1, MAX_PLAN_REV)
            atomic_write(folder / "plan.json", json.dumps(plan).encode("utf-8"))
            self._write_meta(folder, replace(record, plan_rev=new_rev))
            return new_rev

    @staticmethod
    def _write_meta(folder: Path, record: ExportRecord) -> None:
        atomic_write(folder / "meta.json", json.dumps(record.to_json()).encode("utf-8"))

    # -- files ------------------------------------------------------------------------------
    def path_of(self, owner: str, file_id: str, name: str) -> Path:
        if name not in DATA_FILES and name not in SOURCE_FILES.values():
            raise ExportNotFound()
        return self.folder(owner, file_id) / name

    def read_bytes(self, owner: str, file_id: str, name: str) -> bytes:
        path = self.path_of(owner, file_id, name)
        try:
            if path.is_symlink():
                raise OSError("symlink")
            return path.read_bytes()
        except OSError as exc:
            raise ExportNotFound("sections") from exc

    def write_bytes(self, owner: str, file_id: str, name: str, data: bytes) -> None:
        with self._lock:
            atomic_write(self.path_of(owner, file_id, name), data)

    def read_json(self, owner: str, file_id: str, name: str) -> Any:
        path = self.path_of(owner, file_id, name)
        try:
            return json.loads(_read_capped(path, MAX_JSON_BYTES))
        except (OSError, ValueError, RecursionError) as exc:
            raise ExportNotFound("sections") from exc

    def write_json(self, owner: str, file_id: str, name: str, value: Any) -> None:
        self.write_bytes(owner, file_id, name, json.dumps(value).encode("utf-8"))

    def drop(self, owner: str, file_id: str, *names: str) -> None:
        with self._lock:
            for name in names:
                with suppress(OSError):
                    self.path_of(owner, file_id, name).unlink()

    # -- quotas -----------------------------------------------------------------------------
    @staticmethod
    def _folder_bytes(folder: Path) -> int:
        total = 0
        with suppress(OSError):
            for entry in folder.iterdir():
                with suppress(OSError):
                    if entry.is_file() and not entry.is_symlink():
                        total += entry.stat().st_size
        return total

    def _owner_usage(self, owner: str) -> tuple[int, int]:
        folders = list(self._iter_dirs(self._owner_dir(owner)))
        return len(folders), sum(self._folder_bytes(f) for f in folders)

    def _global_bytes(self) -> int:
        total = 0
        with suppress(OSError):
            for owner_dir in self.root.iterdir():
                if owner_dir.is_dir() and not owner_dir.name.startswith("."):
                    total += sum(self._folder_bytes(f) for f in self._iter_dirs(owner_dir))
        return total

    def quota(self, owner: str) -> dict[str, int]:
        files, used = self._owner_usage(owner)
        return {
            "used": used,
            "max_bytes": self.user_max_bytes,
            "files": files,
            "max_files": self.user_max_files,
        }

    def _check_quota(self, owner: str, extra: int) -> None:
        files, used = self._owner_usage(owner)
        need = extra * RESERVE
        if files >= self.user_max_files or used + need > self.user_max_bytes:
            raise ExportLimitError("quota")
        if self._global_bytes() + need > self.global_max_bytes:
            raise ExportLimitError("quota")

    # -- upload -----------------------------------------------------------------------------
    def uploads(self, owner: str) -> OwnerExportUploads:
        return OwnerExportUploads(self, owner)

    def begin_upload(self, owner: str, filename: str) -> ExportUploadSink:
        """Validate the name, refuse early when the user is full, open a staging file."""
        name, kind = clean_name(filename)
        with self._lock:
            try:
                self._check_quota(owner, 0)
            except ExportFailure as exc:
                raise UploadError(exc.status, str(exc)) from exc
        try:
            _private_dirs(self.temp_dir)
            self._remove_stale_temp(STALE_TEMP_SECONDS)
            fd, temp = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX, dir=self.temp_dir)
        except OSError as exc:
            log.warning("cannot create an export upload file (%s)", type(exc).__name__)
            raise UploadError(507, "the file could not be stored") from exc
        return ExportUploadSink(owner, name, kind, os.fdopen(fd, "wb"), Path(temp))

    def commit(self, staged: StagedExport, *, engine: str = "auto") -> ExportRecord:
        """Move a staged upload into place under the lock (quotas are exact), write its meta."""
        record = ExportRecord(
            id=uuid.uuid4().hex,
            name=staged.name,
            kind=staged.kind,
            size=staged.size,
            created=self._clock(),
            engine=engine,
        )
        with self._lock:
            self._check_quota(staged.owner, staged.size)
            folder = self._owner_dir(staged.owner) / record.id
            _private_dirs(folder)
            try:
                os.replace(staged.path, folder / record.source_name)
                os.chmod(folder / record.source_name, 0o600)
                self._write_meta(folder, record)
            except OSError:
                shutil.rmtree(folder, ignore_errors=True)
                raise
        return record

    def delete(self, owner: str, file_id: str) -> None:
        """Remove the whole directory of a file (never anything outside the store)."""
        with self._lock:
            folder = self.folder(owner, file_id)
            if folder.resolve().parent.parent != self.root.resolve():
                raise ExportNotFound()
            shutil.rmtree(folder)

    # -- start-up ---------------------------------------------------------------------------
    def recover(self) -> None:
        """Drop staging leftovers; an analysis a dead process left running becomes an error."""
        with self._lock:
            self._remove_stale_temp(0)
            try:
                owner_dirs = [d for d in self.root.iterdir() if d.is_dir()]
            except OSError:
                return
            for owner_dir in owner_dirs:
                if not owner_dir.name.startswith("."):
                    for folder in self._iter_dirs(owner_dir):
                        self._recover_folder(folder)

    def _remove_stale_temp(self, max_age: float) -> None:
        cutoff = time.time() - max_age
        with suppress(OSError):
            for path in self.temp_dir.glob(f"{TEMP_PREFIX}*{TEMP_SUFFIX}"):
                with suppress(OSError):
                    if path.is_file() and (max_age <= 0 or path.stat().st_mtime < cutoff):
                        path.unlink()

    def _recover_folder(self, folder: Path) -> None:
        with suppress(OSError):
            for path in folder.glob(".*.tmp"):
                path.unlink()
        try:
            record = self._read_meta(folder)
        except ExportNotFound:
            return
        if record.running:
            with suppress(OSError):
                self._write_meta(folder, replace(record, state="error", error_code=INTERRUPTED))


def _read_capped(path: Path, limit: int) -> bytes:
    if path.is_symlink():
        raise OSError("symlink")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file too large")
    return data


__all__ = [
    "INTERRUPTED",
    "MAX_EXPORT_DOCX_BYTES",
    "MAX_EXPORT_PDF_BYTES",
    "ExportRecord",
    "ExportStore",
    "ExportUploadSink",
    "StagedExport",
    "atomic_write",
    "clean_name",
]
