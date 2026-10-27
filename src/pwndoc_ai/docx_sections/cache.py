"""A small on-disk cache of detection results, keyed by the DOCX hash and the detection mode.

Key = sha256(docx) + AI on/off + model + SECTIONS_VERSION (EXPORT_DESIGN.md (h)). Entries are
re-validated on read (``DocumentSections.from_json``): a tampered or damaged entry is a miss and
is removed. Directories are 0700, files 0600, writes are atomic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path

from .model import SECTIONS_VERSION, DocumentSections

log = logging.getLogger(__name__)

HEX64 = re.compile(r"[0-9a-f]{64}")
KEY = re.compile(r"[0-9a-f]{32}")
MAX_ENTRY_BYTES = 64 * 1024 * 1024


def cache_key(ai_on: bool, model: str) -> str:
    """The mode part of the key (the document hash is passed separately)."""
    raw = json.dumps(["ai" if ai_on else "heuristic", model if ai_on else "", SECTIONS_VERSION])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class SectionsCache:
    """``<root>/<doc_sha256>/<key>.json``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, doc_sha256: str, key: str) -> Path | None:
        if not HEX64.fullmatch(doc_sha256 or "") or not KEY.fullmatch(key or ""):
            return None
        return self.root / doc_sha256 / f"{key}.json"

    def get(self, doc_sha256: str, key: str) -> DocumentSections | None:
        path = self._path(doc_sha256, key)
        if path is None or not path.is_file():
            return None
        try:
            if path.stat().st_size > MAX_ENTRY_BYTES:
                raise ValueError("entry too large")
            found = DocumentSections.from_json(json.loads(path.read_bytes()))
            if found.doc_sha256 != doc_sha256:
                raise ValueError("hash mismatch")
        except (OSError, ValueError) as exc:
            log.warning("sections cache entry dropped (%s)", type(exc).__name__)
            path.unlink(missing_ok=True)
            return None
        return found

    def put(self, doc_sha256: str, key: str, result: DocumentSections) -> bool:
        """Store ``result``; returns False when the key is invalid or the write failed."""
        path = self._path(doc_sha256, key)
        if path is None or result.doc_sha256 != doc_sha256:
            return False
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            payload = json.dumps(result.to_json(), ensure_ascii=False).encode()
            fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                os.chmod(temp, 0o600)
                os.replace(temp, path)
            except BaseException:
                Path(temp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            log.warning("sections cache write failed (%s)", type(exc).__name__)
            return False
        return True
