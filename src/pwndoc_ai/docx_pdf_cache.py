"""A small in-memory cache of produced PDFs, so downloading the same file again does not start
LibreOffice again (each conversion is slow and one of only a few slots).

Keyed by the SHA-256 of the Word bytes: a new refine run produces other bytes and so a fresh
conversion, and nothing is written to disk. Bounded by entry count and total bytes; the least
recently used entries go first. Only successful conversions are stored. An entry may carry the
small ``facts`` the renderer reported (page count, blank pages removed...), so a repeat download
can answer with the same headers. The key names the variant too (tidied or not): the same Word
bytes rendered two ways are two entries.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

MAX_ENTRIES = 8
MAX_BYTES = 64 * 1024 * 1024


class PdfCache:
    def __init__(self, max_entries: int = MAX_ENTRIES, max_bytes: int = MAX_BYTES) -> None:
        self._max_entries, self._max_bytes = max_entries, max_bytes
        self._items: OrderedDict[str, tuple[bytes, dict[str, Any]]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    @staticmethod
    def key(docx: bytes, variant: str = "") -> str:
        digest = sha256(docx).hexdigest()
        return f"{digest}:{variant}" if variant else digest

    def entry(self, key: str) -> tuple[bytes, dict[str, Any]] | None:
        """The cached PDF and a copy of its facts, or None."""
        with self._lock:
            found = self._items.get(key)
            if found is None:
                return None
            self._items.move_to_end(key)
            return found[0], dict(found[1])

    def get(self, key: str) -> bytes | None:
        found = self.entry(key)
        return None if found is None else found[0]

    def put(self, key: str, pdf: bytes, facts: Mapping[str, Any] | None = None) -> None:
        if len(pdf) > self._max_bytes:
            return
        with self._lock:
            dropped = self._items.pop(key, None)
            self._bytes -= len(dropped[0]) if dropped else 0
            self._items[key] = (pdf, dict(facts or {}))
            self._bytes += len(pdf)
            while len(self._items) > self._max_entries or self._bytes > self._max_bytes:
                _, (oldest, _facts) = self._items.popitem(last=False)
                self._bytes -= len(oldest)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0
