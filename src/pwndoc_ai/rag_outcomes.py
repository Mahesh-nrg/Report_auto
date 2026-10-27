"""Why a stored document is not indexed: remembered next to the index.

A file can be stored and still not have a single chunk: it is a duplicate of another file, or it
has no readable text, or it is too large. Without a record such a file looks "pending" for ever
and every indexing run reads it again (a scanned PDF could be OCR'd again for ten minutes each
time). A record is ``(status, error, sha of the bytes, size, mtime)``; it counts only while the
file still has those bytes, so a replaced file is looked at afresh.

The records live in one small JSON file (0600, written atomically) and are validated when read.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)

OUTCOMES_FILE = "outcomes.json"
MAX_OUTCOMES = 20000
MAX_ERROR_CHARS = 200
MAX_FILE_BYTES = 8 * 1024 * 1024
STATUSES = ("duplicate", "failed")


class Outcome(NamedTuple):
    status: str  # duplicate | failed
    error: str
    sha: str
    size: int
    mtime_ns: int
    ocr: bool  # OCR was available when a scanned PDF failed


def _parse(raw: object) -> Outcome | None:
    if not isinstance(raw, dict):
        return None
    status, error, sha = raw.get("status"), raw.get("error", ""), raw.get("sha")
    size, mtime, ocr = raw.get("size"), raw.get("mtime_ns"), raw.get("ocr", False)
    if status not in STATUSES or not isinstance(error, str) or not isinstance(sha, str):
        return None
    if any(isinstance(n, bool) or not isinstance(n, int) for n in (size, mtime)):
        return None
    return Outcome(status, error[:MAX_ERROR_CHARS], sha, size, mtime, ocr is True)


class OutcomeStore:
    """The records of one index (``<index dir>/outcomes.json``)."""

    def __init__(self, folder: Path):
        self._path = Path(folder) / OUTCOMES_FILE

    def load(self) -> dict[str, Outcome]:
        """Every valid record; an unreadable or hand-edited file just reads as empty."""
        try:
            if self._path.stat().st_size > MAX_FILE_BYTES:
                return {}
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("ignoring an unreadable document outcome file: %s", type(exc).__name__)
            return {}
        files = raw.get("files") if isinstance(raw, dict) else None
        if not isinstance(files, dict):
            return {}
        parsed = ((name, _parse(entry)) for name, entry in list(files.items())[:MAX_OUTCOMES])
        return {name: o for name, o in parsed if isinstance(name, str) and o is not None}

    def get(self, name: str, sha: str) -> Outcome | None:
        """The record of ``name`` if it was made for bytes with this hash."""
        found = self.load().get(name)
        return found if found is not None and found.sha == sha else None

    def current(self, docs_dir: Path) -> dict[str, Outcome]:
        """The records that still describe the files in ``docs_dir`` (same size and mtime)."""
        valid: dict[str, Outcome] = {}
        for name, outcome in self.load().items():
            try:
                info = (Path(docs_dir) / name).stat()
            except OSError:
                continue
            if info.st_size == outcome.size and info.st_mtime_ns == outcome.mtime_ns:
                valid[name] = outcome
        return valid

    def record(self, name: str, outcome: Outcome) -> None:
        records = self.load()
        if records.get(name) != outcome:
            records[name] = outcome
            self._save(records)

    def clear(self, name: str) -> None:
        records = self.load()
        if records.pop(name, None) is not None:
            self._save(records)

    def _save(self, records: dict[str, Outcome]) -> None:
        payload = {"files": {n: o._asdict() for n, o in sorted(records.items())}}
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=self._path.parent, prefix=".outcomes-", suffix=".part")
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                os.replace(temp, self._path)
            except BaseException:
                Path(temp).unlink(missing_ok=True)
                raise
        except OSError as exc:  # a note that cannot be written only costs a re-read later
            log.warning("could not save the document outcomes: %s", type(exc).__name__)
