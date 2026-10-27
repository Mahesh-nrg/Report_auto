"""Private, quota-limited storage of uploaded Word reports and their refined copies.

Layout (everything below ``<data_dir>/docx``, directories 0700, files 0600)::

    .tmp/.upload-*.part                      uploads in flight (never a user document)
    <sha256(username)[:32]>/<uuid4 hex>/
        original.docx   refined.docx   tracked.docx   meta.json   changes.json

The username never appears in a path, and a path is built only from a validated uuid4 hex id
under the hash directory of the asking user, so another user's id simply does not exist for them
(:class:`DocxNotFound`, which the API turns into a 404). ``meta.json`` carries the state machine
(``uploaded``/``running``/``done``/``error``/``cancelled``); it is replaced atomically and a run
left ``running`` by a dead process is turned into ``error: interrupted`` when the store opens.

An upload is streamed into a temporary file with a hard size cap counted on the bytes received,
checked by magic bytes and a cheap structural pre-check (hostile zips, macros), and only moved into
place by :meth:`DocxStore.commit` under the store lock, which is what makes the quotas exact.
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
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, BinaryIO

from .rag import safe_filename
from .uploads import (
    HTTP_BAD_REQUEST,
    HTTP_STORAGE_FULL,
    HTTP_TOO_LARGE,
    HTTP_UNSUPPORTED,
    STALE_TEMP_SECONDS,
    UploadError,
    lock_for,
)

log = logging.getLogger(__name__)

MAX_DOCX_BYTES = 50 * 1024 * 1024  # one uploaded report
USER_MAX_FILES = 25
USER_MAX_BYTES = 1024**3
GLOBAL_MAX_BYTES = 5 * 1024**3
RESULT_RESERVE = 3  # an upload must leave room for itself plus the refined and tracked copies
MAX_CHANGES = 2000  # entries kept for the preview route
MAX_CHANGE_TEXT = 4000
MAX_META_BYTES = 1024 * 1024
META_HEADROOM = 64 * 1024  # what a later update (run summary, state) may add to a full meta
TITLE_WIDTHS = (80, 40, 20)  # outline titles are cut to these before outline rows are dropped
DAMAGED = "damaged"
DAMAGED_NAME = "Damaged document"
MAX_OWNER_ITEMS = 500  # directories read per user (a bound, far above the file quota)

ROOT_NAME = "docx"
TEMP_NAME = ".tmp"
TEMP_PREFIX = ".upload-"
TEMP_SUFFIX = ".part"
ORIGINAL, REFINED, TRACKED = "original.docx", "refined.docx", "tracked.docx"
META, CHANGES = "meta.json", "changes.json"
KIND_FILES = {"original": ORIGINAL, "clean": REFINED, "tracked": TRACKED}
RESULT_FILES = (REFINED, TRACKED, CHANGES)
STATES = ("uploaded", "running", "done", "error", "cancelled")
FILE_ID = re.compile(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}")  # uuid4().hex
DOCX_MAGIC = b"PK\x03\x04"
MAGIC_BYTES = 4
INTERRUPTED = "interrupted: the gateway restarted while the run was in progress"

# structural pre-check of the zip (the engine validates the content itself afterwards)
MAX_ENTRIES = 5000
MAX_EXPANDED = 300 * 1024 * 1024
RATIO_FROM_BYTES = 1024 * 1024  # the ratio rule only applies to entries at least this large
MAX_RATIO = 500
MAX_CONTENT_TYPES = 1024 * 1024
REQUIRED_PARTS = ("[Content_Types].xml", "word/document.xml")
DRIVE_PREFIX = re.compile(r"[A-Za-z]:")
BAD_DOCX = "the file is not a valid .docx document"
NO_MACROS = "documents with macros are not accepted"

FULL_FILES = "You have reached the limit of {n} documents; delete some files and try again."
FULL_USER = (
    "Not enough storage left for this document and its refined copies; delete some files and "
    "try again."
)
FULL_SERVER = (
    "The server's document storage is full; delete some files (or ask an administrator) and "
    "try again."
)


class DocxNotFound(LookupError):
    """No such file for this user (it may belong to somebody else: the two look the same)."""


def owner_hash(owner: str) -> str:
    return sha256(owner.encode("utf-8")).hexdigest()[:32]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_private_dirs(directory: Path) -> None:
    missing = [d for d in (directory, *directory.parents) if not d.exists()]
    for folder in reversed(missing):
        folder.mkdir(mode=0o700, exist_ok=True)


def _atomic_write(target: Path, data: bytes) -> None:
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


def _clean_name(filename: str) -> str:
    """The stored name for an uploaded file name (400 / 415 when it cannot be accepted)."""
    if not filename or any(ord(c) < 32 or ord(c) == 127 for c in filename):
        raise UploadError(HTTP_BAD_REQUEST, "the file name is not valid")
    name = safe_filename(filename)
    if Path(name).suffix.lower() != ".docx":
        raise UploadError(HTTP_UNSUPPORTED, "only .docx Word documents can be uploaded")
    return name


# -- the record -------------------------------------------------------------------------------
@dataclass(frozen=True)
class DocxRecord:
    """One stored document as ``meta.json`` describes it. Never mutated: use ``replace``."""

    id: str
    owner: str
    name: str
    size: int
    created: str
    state: str = "uploaded"
    mode: str = ""
    changed: int = 0
    rejected: int = 0
    protected: int = 0
    error: str = ""
    started: str | None = None
    ended: str | None = None
    total: int = 0
    done: int = 0
    current: str = ""
    stats: dict[str, int] = field(default_factory=dict)
    outline: list[dict[str, Any]] = field(default_factory=list)
    run: dict[str, Any] = field(default_factory=dict)

    def item(self) -> dict[str, Any]:
        """The entry of the file list (what the contract calls the list item)."""
        return {
            "id": self.id,
            "name": self.name,
            "size": self.size,
            "created": self.created,
            "state": self.state,
            "mode": self.mode,
            "changed": self.changed,
            "rejected": self.rejected,
            "protected": self.protected,
        }

    def detail(self) -> dict[str, Any]:
        return {
            **self.item(),
            "outline": [dict(o) for o in self.outline],
            "stats": dict(self.stats),
            "last_run": {
                **self.run,
                "state": self.state,
                "mode": self.mode,
                "error": self.error,
                "started": self.started,
                "ended": self.ended,
            },
        }


_INT_FIELDS = ("size", "changed", "rejected", "protected", "total", "done")
_STR_FIELDS = ("id", "owner", "name", "created", "mode", "error", "current")


def _record_from(data: Any) -> DocxRecord:
    """Validate a loaded ``meta.json`` (it is only as trustworthy as the disk)."""
    if not isinstance(data, dict):
        raise ValueError("meta is not an object")
    clean: dict[str, Any] = {}
    for key in _STR_FIELDS:
        clean[key] = data.get(key, "")
        if not isinstance(clean[key], str):
            raise ValueError(f"meta field {key} is not text")
    for key in _INT_FIELDS:
        value = data.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"meta field {key} is not a count")
        clean[key] = value
    state = data.get("state", "uploaded")
    if state not in STATES or not FILE_ID.fullmatch(clean["id"]) or not clean["owner"]:
        raise ValueError("meta state or id is invalid")
    for key in ("started", "ended"):
        value = data.get(key)
        clean[key] = value if isinstance(value, str) else None
    for key, kind in (("stats", dict), ("outline", list), ("run", dict)):
        value = data.get(key, kind())
        clean[key] = value if isinstance(value, kind) else kind()
    return DocxRecord(state=state, **clean)


def _meta_bytes(record: DocxRecord) -> bytes:
    return json.dumps(asdict(record), ensure_ascii=False).encode("utf-8")


def fit_record(record: DocxRecord) -> DocxRecord:
    """``record`` with its outline shortened (titles first, then rows) until its meta.json stays
    readable: a meta over ``MAX_META_BYTES`` is refused on read, which would hide the document
    from its owner. Raises a 400 :class:`UploadError` when even an empty outline does not fit."""
    limit = MAX_META_BYTES - META_HEADROOM
    if len(_meta_bytes(record)) <= limit:
        return record
    for width in TITLE_WIDTHS:
        outline = [{**o, "title": str(o.get("title", ""))[:width]} for o in record.outline]
        shorter = replace(record, outline=outline)
        if len(_meta_bytes(shorter)) <= limit:
            return shorter
    outline = shorter.outline
    low, high = 0, len(outline)
    while low < high:  # the most rows that fit
        middle = (low + high + 1) // 2
        if len(_meta_bytes(replace(shorter, outline=outline[:middle]))) <= limit:
            low = middle
        else:
            high = middle - 1
    result = replace(shorter, outline=outline[:low])
    if len(_meta_bytes(result)) > limit:
        raise UploadError(HTTP_BAD_REQUEST, "the document is too large or complex")
    return result


# -- the upload -------------------------------------------------------------------------------
@dataclass(frozen=True)
class StagedUpload:
    """A fully received, pre-checked file that is not stored yet (see ``DocxStore.commit``)."""

    owner: str
    name: str
    size: int
    path: Path

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()

    def discard(self) -> None:
        with suppress(OSError):
            self.path.unlink()


def _check_entry_name(name: str) -> None:
    parts = name.split("/")
    if (
        "\x00" in name
        or "\\" in name
        or name.startswith("/")
        or DRIVE_PREFIX.match(name)
        or ".." in parts
    ):
        raise UploadError(HTTP_BAD_REQUEST, BAD_DOCX)


def _check_entries(infos: Sequence[zipfile.ZipInfo]) -> None:
    if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_EXPANDED:
        raise UploadError(HTTP_BAD_REQUEST, "the document is too large or complex")
    seen: set[str] = set()
    for info in infos:
        _check_entry_name(info.filename)
        folded = info.filename.casefold()
        if folded in seen or info.flag_bits & 0x1:  # duplicate name, or encrypted
            raise UploadError(HTTP_BAD_REQUEST, BAD_DOCX)
        seen.add(folded)
        if folded.endswith("vbaproject.bin"):
            raise UploadError(HTTP_BAD_REQUEST, NO_MACROS)
        if info.file_size >= RATIO_FROM_BYTES and info.file_size > MAX_RATIO * max(
            1, info.compress_size
        ):
            raise UploadError(HTTP_BAD_REQUEST, "the document is too large or complex")
    if not set(REQUIRED_PARTS) <= {i.filename for i in infos}:
        raise UploadError(HTTP_BAD_REQUEST, BAD_DOCX)


def precheck_docx(path: Path) -> None:
    """Cheap structural checks on the received file; the engine inspects the content later."""
    try:
        with zipfile.ZipFile(path) as archive:
            _check_entries(archive.infolist())
            with archive.open("[Content_Types].xml") as handle:
                types = handle.read(MAX_CONTENT_TYPES).lower()
    except (zipfile.BadZipFile, OSError, KeyError, RuntimeError, NotImplementedError) as exc:
        raise UploadError(HTTP_BAD_REQUEST, BAD_DOCX) from exc
    if b"macroenabled" in types:
        raise UploadError(HTTP_BAD_REQUEST, NO_MACROS)


class DocxUploadSink:
    """Receives one file's bytes in order, enforcing the cap and the magic as they arrive."""

    def __init__(self, store: DocxStore, owner: str, name: str, handle: BinaryIO, temp: Path):
        self._store, self._owner, self._name = store, owner, name
        self._handle: BinaryIO | None = handle
        self._temp = temp
        self._written = 0
        self._head = b""

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._written += len(chunk)
        if self._written > self._store.max_file_bytes:
            raise UploadError(
                HTTP_TOO_LARGE, f"file is larger than {self._store.max_file_bytes // 2**20} MB"
            )
        if len(self._head) < MAGIC_BYTES:
            self._head += chunk[: MAGIC_BYTES - len(self._head)]
            if len(self._head) >= MAGIC_BYTES and self._head != DOCX_MAGIC:
                raise UploadError(HTTP_BAD_REQUEST, BAD_DOCX)
        try:
            assert self._handle is not None  # noqa: S101 - never called after finish()
            self._handle.write(chunk)
        except OSError as exc:
            log.warning("cannot write a docx upload (%s)", exc.strerror or "I/O error")
            raise UploadError(HTTP_STORAGE_FULL, "the file could not be stored") from exc

    def finish(self) -> StagedUpload:
        """Close and check the file; it stays staged (the caller commits or discards it)."""
        try:
            self._close()
            if self._written == 0:
                raise UploadError(HTTP_BAD_REQUEST, "the file is empty")
            if self._head != DOCX_MAGIC:
                raise UploadError(HTTP_BAD_REQUEST, BAD_DOCX)
            precheck_docx(self._temp)
        except BaseException:
            self.abort()
            raise
        return StagedUpload(self._owner, self._name, self._written, self._temp)

    def abort(self) -> None:
        """Drop the temporary file (safe to call twice; a staged file is dropped as well)."""
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
            log.warning("cannot finish a docx upload (%s)", exc.strerror or "I/O error")
            raise UploadError(HTTP_STORAGE_FULL, "the file could not be stored") from exc


class OwnerUploads:
    """What the streaming reader needs: ``begin(filename)`` -> a sink, for one user."""

    def __init__(self, store: DocxStore, owner: str):
        self._store, self._owner = store, owner

    def begin(self, filename: str) -> DocxUploadSink:
        return self._store.begin_upload(self._owner, filename)


# -- the store --------------------------------------------------------------------------------
class DocxStore:
    def __init__(
        self,
        data_dir: str | Path,
        *,
        max_file_bytes: int = MAX_DOCX_BYTES,
        user_max_files: int = USER_MAX_FILES,
        user_max_bytes: int = USER_MAX_BYTES,
        global_max_bytes: int = GLOBAL_MAX_BYTES,
        max_changes: int = MAX_CHANGES,
        clock: Callable[[], str] = _now,
    ):
        self.root = Path(data_dir) / ROOT_NAME
        self.max_file_bytes = max_file_bytes
        self.user_max_files = user_max_files
        self.user_max_bytes = user_max_bytes
        self.global_max_bytes = global_max_bytes
        self.max_changes = max_changes
        self._clock = clock
        self._lock = lock_for(self.root)
        self.recover()

    # -- paths ------------------------------------------------------------------------------
    @property
    def temp_dir(self) -> Path:
        return self.root / TEMP_NAME

    def _owner_dir(self, owner: str) -> Path:
        return self.root / owner_hash(owner)

    def _item_dir(self, owner: str, file_id: str) -> Path:
        """The directory of ``file_id`` for ``owner``; :class:`DocxNotFound` when absent."""
        if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
            raise DocxNotFound(file_id)
        folder = self._owner_dir(owner) / file_id
        if folder.is_symlink() or not folder.is_dir():
            raise DocxNotFound(file_id)
        return folder

    def _iter_dirs(self, owner_dir: Path) -> Iterator[Path]:
        try:
            entries = sorted(owner_dir.iterdir())[:MAX_OWNER_ITEMS]
        except OSError:
            return
        for path in entries:
            if FILE_ID.fullmatch(path.name) and path.is_dir() and not path.is_symlink():
                yield path

    # -- reading ----------------------------------------------------------------------------
    @staticmethod
    def _read_meta(folder: Path) -> DocxRecord:
        meta = folder / META
        try:
            if meta.stat().st_size > MAX_META_BYTES:
                raise ValueError("meta too large")
            return _record_from(json.loads(meta.read_text(encoding="utf-8")))
        except FileNotFoundError as exc:
            raise DocxNotFound(folder.name) from exc
        except (OSError, ValueError) as exc:
            log.warning("unreadable docx metadata (%s)", type(exc).__name__)
            raise DocxNotFound(folder.name) from exc

    def get(self, owner: str, file_id: str) -> DocxRecord:
        folder = self._item_dir(owner, file_id)
        record = self._read_meta(folder)
        if record.owner != owner or record.id != file_id:
            raise DocxNotFound(file_id)
        return record

    def list(self, owner: str) -> list[DocxRecord]:
        """The owner's documents, newest first (unreadable ones are skipped)."""
        records = []
        for folder in self._iter_dirs(self._owner_dir(owner)):
            with suppress(DocxNotFound):
                record = self._read_meta(folder)
                if record.owner == owner and record.id == folder.name:
                    records.append(record)
        return sorted(records, key=lambda r: r.created, reverse=True)

    def damaged(self, owner: str) -> list[dict[str, Any]]:
        """The owner's directories whose meta.json is missing or unreadable (``id`` and ``size``).

        They still count against the quota, so the owner must be able to see and delete them.
        """
        found = []
        with self._lock:  # a commit in progress has a directory and no meta yet
            for folder in self._iter_dirs(self._owner_dir(owner)):
                try:
                    self._read_meta(folder)
                except DocxNotFound:
                    found.append({"id": folder.name, "size": self._folder_bytes(folder)})
        return found

    def adopt(self, legacy_owner: str, owner: str) -> int:
        """Move the documents stored under ``legacy_owner`` (a user name) to ``owner`` (an
        account key), rewriting their metadata. Returns how many moved; a folder that cannot be
        moved stays where it is. Only ever called for the account that currently holds the name."""
        if legacy_owner == owner:
            return 0
        source, target = self._owner_dir(legacy_owner), self._owner_dir(owner)
        moved = 0
        with self._lock:
            for folder in self._iter_dirs(source):
                destination = target / folder.name
                try:
                    record = self._read_meta(folder)
                    if record.owner != legacy_owner or destination.exists():
                        continue
                    _make_private_dirs(target)
                    os.rename(folder, destination)
                    self._write_meta(destination, replace(record, owner=owner))
                    moved += 1
                except (DocxNotFound, OSError) as exc:
                    log.warning("cannot move a docx to its account (%s)", type(exc).__name__)
            with suppress(OSError):
                source.rmdir()  # only when it is empty now
        return moved

    def purge(self, owner: str) -> int:
        """Delete every document of ``owner`` (an account that no longer exists). Returns how
        many were removed. Nothing outside the owner's own hash directory is touched."""
        removed = 0
        with self._lock:
            for folder in self._iter_dirs(self._owner_dir(owner)):
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
            with suppress(OSError):
                self._owner_dir(owner).rmdir()
        return removed

    def read_file(self, owner: str, file_id: str, kind: str) -> bytes:
        """The bytes of ``original``/``clean``/``tracked`` (DocxNotFound when not there yet)."""
        if kind not in KIND_FILES:
            raise ValueError("unknown file kind")
        path = self._item_dir(owner, file_id) / KIND_FILES[kind]
        try:
            if path.is_symlink() or not path.is_file():
                raise DocxNotFound(file_id)
            return path.read_bytes()
        except OSError as exc:
            raise DocxNotFound(file_id) from exc

    def load_changes(self, owner: str, file_id: str) -> list[dict[str, Any]]:
        path = self._item_dir(owner, file_id) / CHANGES
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            log.warning("unreadable docx change list")
            return []
        return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []

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
            "used_bytes": used,
            "max_bytes": self.user_max_bytes,
            "files": files,
            "max_files": self.user_max_files,
        }

    def _check_quota(self, owner: str, extra: int, *, reserve: int = RESULT_RESERVE) -> None:
        """Raise a 413 :class:`UploadError` when ``extra`` bytes (times ``reserve``) do not fit."""
        files, used = self._owner_usage(owner)
        if files >= self.user_max_files:
            raise UploadError(HTTP_TOO_LARGE, FULL_FILES.format(n=self.user_max_files))
        need = extra * reserve
        if used + need > self.user_max_bytes:
            raise UploadError(HTTP_TOO_LARGE, FULL_USER)
        if self._global_bytes() + need > self.global_max_bytes:
            raise UploadError(HTTP_TOO_LARGE, FULL_SERVER)

    # -- upload -----------------------------------------------------------------------------
    def uploads(self, owner: str) -> OwnerUploads:
        return OwnerUploads(self, owner)

    def begin_upload(self, owner: str, filename: str) -> DocxUploadSink:
        """Validate the name, refuse early when the user is full, open a temporary file."""
        name = _clean_name(filename)
        with self._lock:
            self._check_quota(owner, 0)
        try:
            _make_private_dirs(self.temp_dir)
            self._remove_stale_temp(STALE_TEMP_SECONDS)
            fd, temp = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX, dir=self.temp_dir)
        except OSError as exc:
            log.warning("cannot create a docx upload file (%s)", exc.strerror or "I/O error")
            raise UploadError(HTTP_STORAGE_FULL, "the file could not be stored") from exc
        return DocxUploadSink(self, owner, name, os.fdopen(fd, "wb"), Path(temp))

    def commit(
        self, staged: StagedUpload, *, stats: Mapping[str, int], outline: Sequence[Mapping]
    ) -> DocxRecord:
        """Move a staged upload into place under the lock (quotas are exact), write its meta."""
        file_id = uuid.uuid4().hex
        record = fit_record(
            DocxRecord(
                id=file_id,
                owner=staged.owner,
                name=staged.name,
                size=staged.size,
                created=self._clock(),
                protected=int(stats.get("protected", 0)),
                stats={str(k): int(v) for k, v in stats.items()},
                outline=[dict(o) for o in outline],
            )
        )
        with self._lock:
            self._check_quota(staged.owner, staged.size)
            folder = self._owner_dir(staged.owner) / file_id
            try:
                _make_private_dirs(folder)
                os.replace(staged.path, folder / ORIGINAL)
                self._write_meta(folder, record)
            except OSError as exc:
                shutil.rmtree(folder, ignore_errors=True)
                log.warning("cannot store a docx upload (%s)", exc.strerror or "I/O error")
                raise UploadError(HTTP_STORAGE_FULL, "the file could not be stored") from exc
        return record

    # -- writing ----------------------------------------------------------------------------
    @staticmethod
    def _write_meta(folder: Path, record: DocxRecord) -> None:
        _atomic_write(folder / META, _meta_bytes(fit_record(record)))

    def update(self, owner: str, file_id: str, **changes: Any) -> DocxRecord:
        """A new record with ``changes`` applied (unknown field names raise TypeError)."""
        with self._lock:
            folder = self._item_dir(owner, file_id)
            current = self.get(owner, file_id)
            updated = replace(current, **changes)
            self._write_meta(folder, updated)
        return updated

    def discard_results(self, owner: str, file_id: str) -> None:
        with self._lock:
            folder = self._item_dir(owner, file_id)
            for name in RESULT_FILES:
                with suppress(OSError):
                    (folder / name).unlink()

    def save_result(
        self,
        owner: str,
        file_id: str,
        clean: bytes,
        tracked: bytes,
        changes: Sequence[Mapping[str, Any]],
        record_fields: Mapping[str, Any],
    ) -> DocxRecord:
        """Store both refined files, the change list and the meta (meta last) in one step."""
        kept = [self._capped(c) for c in changes if c.get("status") != "unchanged"]
        kept = kept[: self.max_changes]
        change_bytes = json.dumps(kept, ensure_ascii=False).encode("utf-8")
        with self._lock:
            folder = self._item_dir(owner, file_id)
            current = self.get(owner, file_id)
            self._check_result_fits(owner, folder, len(clean) + len(tracked) + len(change_bytes))
            try:
                _atomic_write(folder / REFINED, clean)
                _atomic_write(folder / TRACKED, tracked)
                _atomic_write(folder / CHANGES, change_bytes)
                updated = replace(current, **{"state": "done", **record_fields})
                self._write_meta(folder, updated)
            except OSError as exc:
                for name in RESULT_FILES:
                    with suppress(OSError):
                        (folder / name).unlink()
                log.warning("cannot store a refined docx (%s)", exc.strerror or "I/O error")
                raise UploadError(HTTP_STORAGE_FULL, "the file could not be stored") from exc
        return updated

    def _check_result_fits(self, owner: str, folder: Path, new_bytes: int) -> None:
        replaced = sum((folder / n).stat().st_size for n in RESULT_FILES if (folder / n).is_file())
        _, used = self._owner_usage(owner)
        if used - replaced + new_bytes > self.user_max_bytes:
            raise UploadError(HTTP_TOO_LARGE, FULL_USER)
        if self._global_bytes() - replaced + new_bytes > self.global_max_bytes:
            raise UploadError(HTTP_TOO_LARGE, FULL_SERVER)

    @staticmethod
    def _capped(change: Mapping[str, Any]) -> dict[str, Any]:
        def cut(value: Any) -> str:
            text = str(value or "")
            return text if len(text) <= MAX_CHANGE_TEXT else text[: MAX_CHANGE_TEXT - 3] + "..."

        return {
            "unit_id": cut(change.get("unit_id"))[:64],
            "section": cut(change.get("section", change.get("section_id")))[:64],
            "before": cut(change.get("before")),
            "after": cut(change.get("after")),
            "status": cut(change.get("status"))[:16],
            "reason": cut(change.get("reason"))[:200],
        }

    def delete(self, owner: str, file_id: str) -> None:
        """Remove the whole directory of a document (never anything outside the store)."""
        with self._lock:
            folder = self._item_dir(owner, file_id)
            root = self.root.resolve()
            if folder.resolve().parent.parent != root:  # belt and braces: never leave the store
                raise DocxNotFound(file_id)
            shutil.rmtree(folder)

    # -- start-up ---------------------------------------------------------------------------
    def recover(self) -> None:
        """Drop leftover temp files and turn a run a dead process left running into an error.

        Only temp files are ever removed here, never a user document. A store is opened once per
        gateway start, so nothing that is ``running`` on disk can still be running.
        """
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
            for path in folder.glob("*.tmp"):
                path.unlink()
        try:
            record = self._read_meta(folder)
        except DocxNotFound:
            return
        if record.state == "running":
            interrupted = replace(record, state="error", error=INTERRUPTED, ended=self._clock())
            with suppress(OSError):
                self._write_meta(folder, interrupted)


__all__ = [
    "DocxNotFound", "DocxRecord", "DocxStore", "DocxUploadSink", "OwnerUploads", "StagedUpload",
    "MAX_DOCX_BYTES", "MAX_META_BYTES", "DAMAGED", "DAMAGED_NAME", "fit_record", "owner_hash",
    "precheck_docx",
]  # fmt: skip
