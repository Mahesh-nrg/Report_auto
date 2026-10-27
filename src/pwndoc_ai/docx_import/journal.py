"""The undo journal: the PwnDoc ids one import created, written the moment PwnDoc returns them.

``journal.json`` lives in the owner's import directory (``store.journal_path``), so owner
isolation comes from the path the store hands out. Each change is a complete, atomic rewrite
(temporary file in the same directory, ``fsync``, ``os.replace``) with mode 0600, so a crash
after any call leaves either the previous journal or the new one, never a torn file. Reads are
size-capped, refuse symlinks and go through the strict ``Journal.from_json``.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import limits
from .errors import ImportInputError, ImportLimitError
from .records import CreatedObject, Journal

MAX_JOURNAL_BYTES = 1024 * 1024  # a full journal (408 objects) is about 50 KiB
FILE_MODE = 0o600
DIR_MODE = 0o700


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _read_capped(path: Path) -> bytes | None:
    try:
        fd = os.open(path, _open_flags())
    except FileNotFoundError:
        return None
    except OSError as exc:  # a symlink (ELOOP) or an unreadable file
        raise ImportInputError("invalid") from exc
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(MAX_JOURNAL_BYTES + 1)
    if len(raw) > MAX_JOURNAL_BYTES:
        raise ImportInputError("invalid")
    return raw


def read_journal(path: Path) -> Journal | None:
    """The stored journal, ``None`` when there is none, ``ImportInputError`` when corrupt."""
    raw = _read_capped(Path(path))
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ImportInputError("invalid") from exc
    return Journal.from_json(value)


def write_journal(path: Path, journal: Journal) -> None:
    """Atomically replace ``path`` with ``journal`` (0600; the directory is created 0700)."""
    target = Path(path)
    target.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    data = json.dumps(journal.to_json(), separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_JOURNAL_BYTES:
        raise ImportLimitError("too_large")
    fd, temp = tempfile.mkstemp(prefix=".journal-", suffix=".tmp", dir=target.parent)
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


class JournalWriter:
    """Appends created ids to one import's journal; every call rewrites the file atomically.

    The in-memory journal is updated before the disk write, so if the disk write fails the
    caller can still roll back from ``current``. An existing journal file is continued, never
    truncated. Nothing is written until the first ``record`` or ``set_state``.
    """

    def __init__(self, path: Path, import_id: str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        existing = read_journal(self._path)
        if existing is not None and existing.import_id != import_id:
            raise ImportInputError("invalid")
        self._journal = existing or Journal(import_id=import_id, state="open", updated=now_stamp())
        Journal.from_json(self._journal.to_json())  # refuse a bad import id up front

    @property
    def current(self) -> Journal:
        return self._journal

    @property
    def path(self) -> Path:
        return self._path

    def record(self, kind: str, object_id: str, audit_id: str = "") -> None:
        stamp = now_stamp()
        entry = CreatedObject.from_json(
            {"kind": kind, "id": object_id, "at": stamp, "audit_id": audit_id}
        )
        with self._lock:
            known = {(o.kind, o.id) for o in self._journal.objects}
            if (entry.kind, entry.id) in known:
                return
            if len(self._journal.objects) >= limits.MAX_JOURNAL_OBJECTS:
                raise ImportLimitError("too_large")
            objects = (*self._journal.objects, entry)
            self._journal = replace(self._journal, objects=objects, updated=stamp)
            write_journal(self._path, self._journal)

    def set_state(self, state: str) -> None:
        if state not in limits.JOURNAL_STATES:
            raise ImportInputError("invalid")
        with self._lock:
            self._journal = replace(self._journal, state=state, updated=now_stamp())
            write_journal(self._path, self._journal)
