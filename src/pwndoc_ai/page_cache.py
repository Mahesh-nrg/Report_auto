"""On-disk cache of the embeddings of web page chunks, so a page is embedded only once.

An entry is keyed by the SHA-256 of the page text, the embedder's vector space and the chunking
parameters (:func:`page_key`), so a changed page, another embedder or other chunk sizes never
reuse stale vectors. How many chunks of a page a finding takes is NOT part of the key: an entry
holds the first N chunks of the page and is extended when a later finding wants more, so the same
page is never embedded again because the search returned more or fewer pages. Entries live as
``<key>.json`` files in a 0700 directory (files 0600, written atomically), expire after ``days``
and the directory is bounded in file count and bytes for as long as the process runs (a cheap
size check every few writes, a full check of every entry now and then). Everything read back is
validated: the files could have been edited on disk.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)

SECONDS_PER_DAY = 86400
CLOCK_SKEW_SECONDS = 300  # an entry "created" further in the future than this is corrupt
MAX_ENTRY_BYTES = 1024 * 1024
MAX_ENTRIES = 1500
MAX_TOTAL_BYTES = 96 * 1024 * 1024
TYPICAL_ENTRY_BYTES = 100 * 1024  # what a page of about 20 chunks takes: the byte headroom unit
MAX_CHUNKS = 64  # per entry; far above the per-page chunk cap, it only bounds hostile files
MAX_DIMENSIONS = 4096
MAX_CHUNK_CHARS = 4000
VECTOR_DECIMALS = 6
LOCK_STRIPES = 64
CHECK_EVERY_WRITES = 20  # a size check after this many writes; headroom keeps the bound strict
PRUNE_INTERVAL_SECONDS = 600.0  # a full check (expiry, corrupt files) at most this seldom
STALE_PART_SECONDS = 3600.0  # a half-written file older than this is the leftover of a crash
PART_FILE = re.compile(r"^\.tmp-.*\.part$")
KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ENTRY_FILE = re.compile(r"^[0-9a-f]{64}\.json$")

EmbedFn = Callable[[Sequence[str]], list[list[float]]]


class CachedPage(NamedTuple):
    """The chunks of one page and one vector per chunk (immutable)."""

    chunks: tuple[str, ...]
    vectors: tuple[tuple[float, ...], ...]


def page_key(text: str, space: str, params: Sequence[object]) -> str:
    """Cache key of a page: its content hash, the vector space and the chunking parameters."""
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    raw = json.dumps([digest, space, list(params)], ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value)


def _valid_vectors(chunks: list[object], vectors: object) -> bool:
    if not isinstance(vectors, list) or len(vectors) != len(chunks):
        return False
    dims = len(vectors[0]) if vectors and isinstance(vectors[0], list) else 0
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dims or not 0 < dims <= MAX_DIMENSIONS:
            return False
        if not all(_is_number(x) for x in vector):
            return False
    return True


def _parse(raw: object, now: float) -> CachedPage | None:
    """The entry in ``raw`` if it is well formed (shapes, sizes, finite numbers), else ``None``."""
    if not isinstance(raw, dict):
        return None
    created, chunks = raw.get("created"), raw.get("chunks")
    if not _is_number(created) or created > now + CLOCK_SKEW_SECONDS:
        return None
    if not isinstance(chunks, list) or not 0 < len(chunks) <= MAX_CHUNKS:
        return None
    if not all(isinstance(c, str) and 0 < len(c) <= MAX_CHUNK_CHARS for c in chunks):
        return None
    vectors = raw.get("vectors")
    if not _valid_vectors(chunks, vectors):
        return None
    return CachedPage(tuple(chunks), tuple(tuple(float(x) for x in v) for v in vectors))


class PageEmbeddingCache:
    """Disk cache for page embeddings; ``root=None`` or ``days<=0`` disables it."""

    def __init__(self, root: Path | None, days: int):
        self._root = root if days > 0 else None
        self._days = days
        self._pruned = False
        self._writes = 0
        self._last_prune = 0.0
        self._prune_lock = threading.Lock()
        self._stripes = tuple(threading.Lock() for _ in range(LOCK_STRIPES))

    @property
    def enabled(self) -> bool:
        return self._root is not None

    # -- reading ----------------------------------------------------------------------------
    def _read(self, path: Path, now: float) -> tuple[float, CachedPage] | None:
        try:
            if path.is_symlink() or path.stat().st_size > MAX_ENTRY_BYTES:
                return None
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable page embedding entry %s: %s", path.name, exc)
            return None
        entry = _parse(raw, now)
        return None if entry is None else (float(raw["created"]), entry)

    def _expired(self, created: float, now: float) -> bool:
        return now - created >= self._days * SECONDS_PER_DAY

    def get(self, key: str) -> CachedPage | None:
        """The cached chunks and vectors of ``key``, or ``None`` (missing, expired or corrupt)."""
        if self._root is None or not KEY_PATTERN.match(key):
            return None
        path = self._root / f"{key}.json"
        now = time.time()
        found = self._read(path, now)
        if found is None:
            return None
        if self._expired(found[0], now):
            self._unlink(path)
            return None
        return found[1]

    @staticmethod
    def _unlink(path: Path) -> bool:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not remove page embedding entry: %s", type(exc).__name__)
            return False
        return True

    # -- writing ----------------------------------------------------------------------------
    def put(self, key: str, chunks: Sequence[str], vectors: Sequence[Sequence[float]]) -> None:
        """Store an entry atomically; a write failure is logged, never raised."""
        if not KEY_PATTERN.match(key):
            raise ValueError("invalid cache key")
        if len(chunks) != len(vectors):
            raise ValueError("one vector per chunk is required")
        if self._root is None:
            return
        self._maintain()
        payload = {
            "created": time.time(),
            "chunks": list(chunks),
            "vectors": [[round(x, VECTOR_DECIMALS) for x in v] for v in vectors],
        }
        try:
            self._write(self._root, key, payload)
        except OSError as exc:
            log.warning("could not write page embedding entry: %s", exc)

    @staticmethod
    def _write(root: Path, key: str, payload: dict[str, object]) -> None:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        fd, tmp_name = tempfile.mkstemp(dir=root, prefix=".tmp-", suffix=".part")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp_name, root / f"{key}.json")
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def embed_page(
        self, key: str, chunks: Sequence[str], embed: EmbedFn, width: int | None = None
    ) -> list[list[float]]:
        """Vectors for ``chunks``: from the cache, or embedded once and then stored.

        An entry is reused for as many leading chunks as it holds, and only the rest is embedded
        and appended. With ``width`` an entry whose vectors have another size (they came from
        another model) is replaced, never compared. Concurrent callers with the same key wait for
        each other, so a page two findings share is embedded by exactly one of them.
        """
        with self._stripes[int(key[:8], 16) % LOCK_STRIPES]:
            cached = self.get(key)
            known: list[list[float]] = []
            if cached is not None and (width is None or len(cached.vectors[0]) == width):
                known = [list(v) for v in cached.vectors[: _common_prefix(cached.chunks, chunks)]]
            if len(known) == len(chunks):
                return known
            fresh = embed(list(chunks[len(known) :]))
            vectors = known + fresh
            self.put(key, chunks, vectors)
            return vectors

    # -- pruning ----------------------------------------------------------------------------
    def _maintain(self) -> None:
        """Keep the directory bounded for the life of the process (called before every write).

        The first write checks everything. After that a stat-only size check runs every
        ``CHECK_EVERY_WRITES`` writes (and shrinks below the bounds, so the headroom absorbs the
        writes in between), and a full check of every entry runs every ``PRUNE_INTERVAL_SECONDS``.
        """
        with self._prune_lock:
            now = time.time()
            self._writes += 1
            if not self._pruned or now - self._last_prune >= PRUNE_INTERVAL_SECONDS:
                self._pruned, self._last_prune, self._writes = True, now, 0
                full = True
            elif self._writes >= CHECK_EVERY_WRITES:
                self._writes, full = 0, False
            else:
                return
        if full:
            self.prune(headroom=1)  # room for the entry about to be written
        else:
            self._enforce_bounds(headroom=CHECK_EVERY_WRITES)

    def prune(self, headroom: int = 0) -> int:
        """Delete expired and corrupt entries, then the oldest ones past the size bounds."""
        root = self._root
        if root is None or not root.is_dir():
            return 0
        now, removed = time.time(), self._remove_stale_parts(root, time.time())
        kept: list[tuple[float, int, Path]] = []
        for path in root.iterdir():
            if not ENTRY_FILE.match(path.name) or not path.is_file():
                continue
            found = self._read(path, now)
            if found is None or self._expired(found[0], now):
                removed += self._unlink(path)
                continue
            try:
                info = path.stat()
            except OSError as exc:
                log.warning("could not inspect page embedding entry: %s", type(exc).__name__)
                continue
            kept.append((info.st_mtime, info.st_size, path))
        return removed + self._shrink(sorted(kept), headroom)

    def _enforce_bounds(self, headroom: int = 0) -> int:
        """Only the count and the bytes (no entry is parsed): drop the oldest past the bounds."""
        root = self._root
        if root is None or not root.is_dir():
            return 0
        removed = self._remove_stale_parts(root, time.time())
        entries: list[tuple[float, int, Path]] = []
        for path in root.iterdir():
            if ENTRY_FILE.match(path.name):
                with suppress(OSError):
                    info = path.stat()
                    entries.append((info.st_mtime, info.st_size, path))
        return removed + self._shrink(sorted(entries), headroom)

    def _remove_stale_parts(self, root: Path, now: float) -> int:
        """Half-written files of a crashed writer; one that is being written now is left alone."""
        removed = 0
        for path in root.iterdir():
            if PART_FILE.match(path.name):
                with suppress(OSError):
                    if path.is_file() and now - path.stat().st_mtime > STALE_PART_SECONDS:
                        removed += self._unlink(path)
        return removed

    def _shrink(self, oldest_first: list[tuple[float, int, Path]], headroom: int = 0) -> int:
        """Remove the oldest entries until the bounds hold with room for ``headroom`` writes."""
        total = sum(size for _, size, _ in oldest_first)
        count, removed = len(oldest_first), 0
        max_count = MAX_ENTRIES - headroom
        max_bytes = MAX_TOTAL_BYTES - headroom * TYPICAL_ENTRY_BYTES
        for _, size, path in oldest_first:
            if count <= max_count and total <= max_bytes:
                break
            if self._unlink(path):
                removed += 1
            count, total = count - 1, total - size
        return removed


def _common_prefix(cached: Sequence[str], wanted: Sequence[str]) -> int:
    """How many leading chunks are the same in both lists."""
    for position, (have, want) in enumerate(zip(cached, wanted, strict=False)):
        if have != want:
            return position
    return min(len(cached), len(wanted))
