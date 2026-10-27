"""On-disk storage of DOCX imports: one directory per import under the owner's hash directory.

Layout (see docs/IMPORTER_DESIGN.md §e)::

    <root>/<owner_hash(owner)>/<import_id>/
        status.json   ImportStatus
        plan.json     ImportPlan (absent until the analyse succeeds)
        journal.json  Journal (written by the writer; absent until the first PwnDoc write)
        images/<image_key>.bin

Directories are 0700 and files 0600. Every write is atomic (temp file plus ``os.replace``) under
one lock per store root. Every read goes through the strict ``from_json`` of the model: a file
that fails it reads as :class:`ImportNotFound`. Only directories directly under the caller's own
hash directory are ever opened, so another owner's import simply does not exist.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..docx_store import owner_hash
from ..uploads import lock_for
from . import limits
from .errors import ImportConflict, ImportLimitError, ImportNotFound
from .model import ImageBlob, ImportPlan, ImportStatus

log = logging.getLogger(__name__)

STATUS_FILE = "status.json"
PLAN_FILE = "plan.json"
JOURNAL_FILE = "journal.json"
IMAGES_DIR = "images"
IMAGE_SUFFIX = ".bin"
MAX_STATUS_BYTES = 256 * 1024
MAX_PLAN_BYTES = 64 * 1024 * 1024
MAX_OWNER_ITEMS = 500  # directories read per owner: a bound far above the import cap
RUNNING_STATES = frozenset({"analysing", "creating", "undoing"})
CREATED_STATES = frozenset({"created", "undoing", "undo_failed"})


def utc_stamp() -> str:
    """Now as an ISO-Z timestamp that passes ``limits.TIMESTAMP``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_private_dirs(directory: Path) -> None:
    missing = [d for d in (directory, *directory.parents) if not d.exists()]
    for folder in reversed(missing):
        folder.mkdir(mode=0o700, exist_ok=True)


def _atomic_write(target: Path, data: bytes) -> None:
    descriptor, temp = tempfile.mkstemp(dir=target.parent, prefix=".", suffix=".tmp")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temp, target)
    except BaseException:
        with suppress(OSError):
            os.unlink(temp)
        raise


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _read_json(path: Path, cap: int) -> Any:
    """The parsed file, or :class:`ImportNotFound` when absent, a link, too large or not JSON."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > cap:
            raise ImportNotFound()
        return json.loads(path.read_bytes())
    except (OSError, ValueError, RecursionError) as exc:
        raise ImportNotFound() from exc


def _folder_bytes(folder: Path) -> int:
    total = 0
    with suppress(OSError):
        for entry in folder.rglob("*"):
            with suppress(OSError):
                if entry.is_file() and not entry.is_symlink():
                    total += entry.stat().st_size
    return total


class ImportStore:
    """Imports of every owner below ``root`` (``<data_dir>/docx_import``)."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._lock = lock_for(self.root)

    # -- paths ------------------------------------------------------------------------------
    def owner_dir(self, owner: str) -> Path:
        return self.root / owner_hash(owner)

    def _item_dir(self, owner: str, import_id: str) -> Path:
        """The directory of an existing import of ``owner``; :class:`ImportNotFound` otherwise."""
        if not isinstance(import_id, str) or not limits.IMPORT_ID.fullmatch(import_id):
            raise ImportNotFound()
        folder = self.owner_dir(owner) / import_id
        if folder.is_symlink() or not folder.is_dir():
            raise ImportNotFound()
        return folder

    def _iter_dirs(self, owner: str) -> Iterator[Path]:
        try:
            entries = sorted(self.owner_dir(owner).iterdir())[:MAX_OWNER_ITEMS]
        except OSError:
            return
        for path in entries:
            if limits.IMPORT_ID.fullmatch(path.name) and path.is_dir() and not path.is_symlink():
                yield path

    def journal_path(self, owner: str, import_id: str) -> Path:
        return self._item_dir(owner, import_id) / JOURNAL_FILE

    def has_journal(self, owner: str, import_id: str) -> bool:
        path = self.journal_path(owner, import_id)
        return path.is_file() and not path.is_symlink()

    # -- status -----------------------------------------------------------------------------
    def create(self, owner: str, status: ImportStatus) -> None:
        """A new import directory holding ``status`` (quota checked under the lock)."""
        if not limits.IMPORT_ID.fullmatch(status.id):
            raise ImportNotFound()
        with self._lock:
            folders = list(self._iter_dirs(owner))
            if len(folders) >= limits.MAX_IMPORTS_PER_OWNER:
                raise ImportConflict("too_many")
            if sum(_folder_bytes(f) for f in folders) >= limits.MAX_OWNER_IMPORT_BYTES:
                raise ImportLimitError("quota")
            folder = self.owner_dir(owner) / status.id
            _make_private_dirs(folder)
            _atomic_write(folder / STATUS_FILE, _json_bytes(status.to_json()))

    @staticmethod
    def _read_status(folder: Path) -> ImportStatus:
        data = _read_json(folder / STATUS_FILE, MAX_STATUS_BYTES)
        try:
            status = ImportStatus.from_json(data)
        except ValueError as exc:  # ImportInputError: a tampered or damaged file
            raise ImportNotFound() from exc
        if status.id != folder.name:
            raise ImportNotFound()
        return status

    def status(self, owner: str, import_id: str) -> ImportStatus:
        return self._read_status(self._item_dir(owner, import_id))

    def save_status(self, owner: str, status: ImportStatus) -> None:
        with self._lock:
            folder = self._item_dir(owner, status.id)
            _atomic_write(folder / STATUS_FILE, _json_bytes(status.to_json()))

    def list(self, owner: str) -> list[ImportStatus]:
        """The owner's imports, newest first (unreadable ones are skipped)."""
        found = []
        for folder in self._iter_dirs(owner):
            with suppress(ImportNotFound):
                found.append(self._read_status(folder))
        return sorted(found, key=lambda s: (s.started, s.id), reverse=True)

    # -- plan -------------------------------------------------------------------------------
    def read_plan(self, owner: str, import_id: str) -> ImportPlan:
        folder = self._item_dir(owner, import_id)
        data = _read_json(folder / PLAN_FILE, MAX_PLAN_BYTES)
        try:
            plan = ImportPlan.from_json(data)
        except ValueError as exc:  # ImportInputError: a tampered or damaged file
            log.warning("unreadable import plan (%s)", type(exc).__name__)
            raise ImportNotFound() from exc
        if plan.import_id != import_id:
            raise ImportNotFound()
        return plan

    def save_plan(self, owner: str, plan: ImportPlan) -> None:
        data = _json_bytes(plan.to_json())
        with self._lock:
            folder = self._item_dir(owner, plan.import_id)
            self._check_room(owner, len(data) - self._size(folder / PLAN_FILE))
            _atomic_write(folder / PLAN_FILE, data)

    # -- images -----------------------------------------------------------------------------
    def save_images(self, owner: str, import_id: str, blobs: Sequence[ImageBlob]) -> None:
        """Store the extracted image bytes (keys checked; the quota covers them all)."""
        if any(not limits.IMAGE_KEY.fullmatch(b.key) for b in blobs):
            raise ImportNotFound("image")
        with self._lock:
            folder = self._item_dir(owner, import_id) / IMAGES_DIR
            self._check_room(owner, sum(len(b.data) for b in blobs))
            _make_private_dirs(folder)
            for blob in blobs:
                _atomic_write(folder / f"{blob.key}{IMAGE_SUFFIX}", blob.data)

    def read_image(self, owner: str, import_id: str, key: str) -> bytes:
        if not isinstance(key, str) or not limits.IMAGE_KEY.fullmatch(key):
            raise ImportNotFound("image")
        path = self._item_dir(owner, import_id) / IMAGES_DIR / f"{key}{IMAGE_SUFFIX}"
        try:
            if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
                raise ImportNotFound("image")
            if path.stat().st_size > limits.MAX_IMAGE_BYTES:
                raise ImportNotFound("image")
            return path.read_bytes()
        except OSError as exc:
            raise ImportNotFound("image") from exc

    # -- quotas -----------------------------------------------------------------------------
    @staticmethod
    def _size(path: Path) -> int:
        try:
            return path.stat().st_size if path.is_file() else 0
        except OSError:
            return 0

    def _check_room(self, owner: str, extra: int) -> None:
        """Caller holds the lock. :class:`ImportLimitError` ("quota") when ``extra`` bytes do
        not fit in the owner's import storage."""
        used = sum(_folder_bytes(f) for f in self._iter_dirs(owner))
        if used + max(0, extra) > limits.MAX_OWNER_IMPORT_BYTES:
            raise ImportLimitError("quota")

    # -- removal and lookups ----------------------------------------------------------------
    def delete(self, owner: str, import_id: str) -> None:
        """Remove the import's directory (never anything outside the owner's hash directory)."""
        with self._lock:
            folder = self._item_dir(owner, import_id)
            if folder.resolve().parent != self.owner_dir(owner).resolve():
                raise ImportNotFound()
            shutil.rmtree(folder)

    def find_created_by_sha(self, owner: str, sha: str, *, exclude: str = "") -> str:
        """The id of the owner's newest import of the same file that reached ``created``."""
        for status in self.list(owner):
            if status.id == exclude or status.state not in CREATED_STATES:
                continue
            with suppress(ImportNotFound):
                if self.read_plan(owner, status.id).file_sha256 == sha:
                    return status.id
        return ""

    def mark_interrupted(self) -> int:
        """Turn every import a dead process left running into ``interrupted``. Called once at
        start-up, when nothing on disk can still be running. Returns how many were changed."""
        changed = 0
        try:
            owner_dirs = [d for d in self.root.iterdir() if d.is_dir() and not d.is_symlink()]
        except OSError:
            return 0
        for owner_dir in owner_dirs:
            changed += self._interrupt_owner(owner_dir)
        return changed

    def _interrupt_owner(self, owner_dir: Path) -> int:
        changed = 0
        for folder in sorted(owner_dir.iterdir())[:MAX_OWNER_ITEMS]:
            if not limits.IMPORT_ID.fullmatch(folder.name) or folder.is_symlink():
                continue
            with suppress(ImportNotFound, OSError):
                status = self._read_status(folder)
                if status.state in RUNNING_STATES:
                    stopped = replace(status, state="interrupted", current="", ended=utc_stamp())
                    with self._lock:
                        _atomic_write(folder / STATUS_FILE, _json_bytes(stopped.to_json()))
                    changed += 1
        return changed


__all__ = ["CREATED_STATES", "RUNNING_STATES", "ImportStore", "utc_stamp"]
