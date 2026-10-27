"""Builds the research context (internet + local documents) handed to the LLM.

Data flow for one finding (``provide(title, excerpt="")``)::

    title + short plain-text excerpt of the draft --> query text
        |
        |-- research cache (per title, excerpt, settings, embedder space/name, chunking)
        |       hit: return it, nothing is fetched or embedded
        |
        |-- internet:  title --> search --> fetch pages (SSRF/size/time guards, web_context)
        |       --> chunk (~800 chars, overlap, boilerplate stripped)       web_rank
        |       --> embed chunks (per-page disk cache: a page is embedded once)  page_cache
        |       --> embed the query, rank by cosine, drop duplicates
        |       --> top chunks within WEB_BUDGET_CHARS, each tagged ``[web: host]``
        |
        |-- local:     DocumentIndex.query_chunks(query) (it embeds the query through the same
        |       embedder) --> chunks tagged ``[document]``
        |
        '-- merge both under their headings, neutralise ``<research>`` tags, cap the length

All embedding goes through the ONE embedder object passed in (the on-demand Ollama server);
this module never creates, starts or stops one. The caller may also pass the built-in ONNX
embedder as ``fallback_embedder`` (same vector space): it ranks the pages when there is no main
embedder and when the main one fails, so pages are ranked either way. The web
ranking and the query embedding of the local documents share ONE deadline and ONE cool-down (a
:class:`~pwndoc_ai.embed_guard.BoundedEmbed` per vector space): after a failure no finding waits
for the same timeout again. Only if no embedder exists or works (the main one and the fallback
fail or are too slow) does the web part fall back to the first characters of each page,
``Research.degraded`` is True and a short warning (no text, no URL) is logged: a report run never
fails because of embedding.
Degraded results caused by a failure are not cached; the "no embedder installed" fallback is,
under a key that differs from the one of a working embedder.

The result is a :class:`Research`: a plain ``str`` (so it drops into any prompt) that also
remembers where it came from. Results are cached on disk per finding so re-running a report
does not repeat web searches or document lookups.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from . import web_rank
from .config import Settings
from .embed_guard import EMBED_TIMEOUT, BoundedEmbed
from .embedder import EmbedderError
from .page_cache import EmbedFn, PageEmbeddingCache
from .rag import DocumentIndex, format_chunks
from .web_context import WebContext, WebPage

log = logging.getLogger(__name__)

MAX_CONTEXT_CHARS = 12000
MAX_EXCERPT_CHARS = 400  # of the draft excerpt that joins the title in the query
CACHE_FORMAT = 2  # bump when the cached text layout changes; old entries are then ignored
PAGE_CACHE_DIR = "embeddings"  # under the research cache dir
SECONDS_PER_DAY = 86400
MAX_CACHE_FILE_BYTES = 256 * 1024
MAX_CACHED_SOURCES = 50
MAX_SOURCE_CHARS = 253  # a DNS name or a document file name
CLOCK_SKEW_SECONDS = 300  # an entry "created" further in the future than this is corrupt
CACHE_FILE = re.compile(r"^[0-9a-f]{64}\.json$")
RESEARCH_TAG = re.compile(r"<(/?)research", re.IGNORECASE)
HTML_TAG = re.compile(r"<[^>]{0,200}>")


class Research(str):
    """Research text that also carries ``.sources`` (document names / web hostnames).

    ``.degraded`` is True when the embedder was wanted but unavailable, so the pages were cut
    to their first characters (or the local search was skipped) instead of ranked.
    """

    sources: tuple[str, ...]
    degraded: bool

    def __new__(
        cls, text: str = "", sources: Iterable[str] = (), degraded: bool = False
    ) -> Research:
        obj = super().__new__(cls, text)
        obj.sources = tuple(sources)
        obj.degraded = bool(degraded)
        return obj


ContextProvider = Callable[..., Research]  # provide(title, excerpt="") -> Research


class _Part(NamedTuple):
    section: str  # "" when this source found nothing
    sources: tuple[str, ...]
    ok: bool  # False when the lookup raised
    degraded: bool = False  # the embedder was wanted but unavailable


def _neutralise(text: str) -> str:
    """Untrusted text must not be able to close the <research> block it is wrapped in."""
    return RESEARCH_TAG.sub(r"&lt;\1research", text)


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


# -- cache ------------------------------------------------------------------------------------
def _cache_key(
    title: str,
    use_web: bool,
    use_rag: bool,
    web_results: int,
    index_version: str = "",
    ranking: str = "",
) -> str:
    """Key of one finding's research; ``ranking`` fingerprints the embedder and chunking."""
    normalised = " ".join(title.lower().split())
    raw = json.dumps(
        [CACHE_FORMAT, normalised, use_web, use_rag, web_results, index_version, ranking],
        ensure_ascii=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ranking_fingerprint(embedder: Any | None, excerpt: str) -> str:
    """Embedder space/name, chunking and caps, plus the excerpt: what a ranking depends on.

    Read at call time, so a changed chunk size or another embedder never reuses an old ranking.
    """
    space = name = "none"
    if embedder is not None:
        name = str(getattr(embedder, "name", "?"))
        space = str(getattr(embedder, "space", "") or name)
    params = [
        web_rank.CHUNK_CHARS,
        web_rank.CHUNK_OVERLAP,
        web_rank.MAX_CHUNKS_PER_PAGE,
        web_rank.MAX_CHUNKS_PER_FINDING,
        web_rank.WEB_BUDGET_CHARS,
        web_rank.NEAR_DUPLICATE,
    ]
    excerpt_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest() if excerpt else ""
    return json.dumps([space, name, params, excerpt_hash])


def _document_stamp(rag: DocumentIndex, doc: object) -> list[object]:
    """Name, chunk count and file size/mtime of one indexed document."""
    name = doc.get("name") if isinstance(doc, dict) else None
    stamp: list[object] = [name, doc.get("chunks") if isinstance(doc, dict) else None]
    docs_dir = getattr(rag, "docs_dir", None)
    if docs_dir is not None and isinstance(name, str):
        try:
            info = (Path(docs_dir) / name).stat()
            stamp += [info.st_size, info.st_mtime_ns]
        except OSError:
            pass  # the index knows it, the stored file is gone: the name alone still counts
    return stamp


def index_version(rag: DocumentIndex | None) -> str | None:
    """A fingerprint of the local document set, so cached excerpts die with their documents.

    ``""`` when there is no index (or it cannot list documents); ``None`` when listing failed,
    meaning the cache must not be trusted this time.
    """
    list_documents = getattr(rag, "list_documents", None)
    if not callable(list_documents):
        return ""
    try:
        stamps = [_document_stamp(rag, doc) for doc in list_documents()]
    except Exception as exc:  # noqa: BLE001 - a broken index just disables the cache
        log.warning("could not fingerprint the document index: %s", exc)
        return None
    return hashlib.sha256(json.dumps(sorted(stamps, key=str), default=str).encode()).hexdigest()


def _parse_entry(raw: object, now: float) -> tuple[float, Research] | None:
    """Validate a decoded cache file; ``None`` if it is not a well-formed entry."""
    if not isinstance(raw, dict):
        return None
    created, text, sources = raw.get("created"), raw.get("text"), raw.get("sources")
    if isinstance(created, bool) or not isinstance(created, int | float):
        return None
    if created > now + CLOCK_SKEW_SECONDS or not isinstance(text, str) or not text:
        return None
    if not isinstance(sources, list) or len(sources) > MAX_CACHED_SOURCES:
        return None
    if not all(isinstance(s, str) for s in sources):
        return None
    # The file is on disk and could have been edited: make it safe again, exactly as when fresh.
    safe_text = _neutralise(text[:MAX_CONTEXT_CHARS])[:MAX_CONTEXT_CHARS]
    clipped = [s[:MAX_SOURCE_CHARS] for s in sources]
    return float(created), Research(safe_text, clipped, raw.get("degraded") is True)


def _load_entry(path: Path, now: float) -> tuple[float, Research] | None:
    try:
        if path.is_symlink() or path.stat().st_size > MAX_CACHE_FILE_BYTES:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable research cache entry %s: %s", path.name, exc)
        return None
    return _parse_entry(raw, now)


def _is_expired(created: float, days: int, now: float) -> bool:
    return now - created >= days * SECONDS_PER_DAY


def _read_cache(path: Path, days: int) -> Research | None:
    now = time.time()
    entry = _load_entry(path, now)
    if entry is None:
        return None
    created, research = entry
    if _is_expired(created, days, now):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not remove expired research cache entry: %s", type(exc).__name__)
        return None
    return research


def _write_cache(path: Path, research: Research) -> None:
    """Atomically write ``research``; the directory is 0700 and the file 0600."""
    cache_dir = path.parent
    cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(cache_dir, 0o700)
    payload = {
        "created": time.time(),
        "text": str(research),
        "sources": list(research.sources),
        "degraded": research.degraded,
    }
    fd, tmp_name = tempfile.mkstemp(dir=cache_dir, prefix=".tmp-", suffix=".part")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def prune_cache(cache_dir: Path, days: int) -> int:
    """Delete expired and corrupt cache entries; returns how many were removed."""
    if days <= 0 or not cache_dir.is_dir():
        return 0
    now, removed = time.time(), 0
    for path in cache_dir.iterdir():
        if not CACHE_FILE.match(path.name) or not path.is_file():
            continue
        entry = _load_entry(path, now)
        if entry is None or _is_expired(entry[0], days, now):
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                log.warning("could not prune %s: %s", path.name, exc)
    return removed


# -- lookups ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Ranking:
    """Everything needed to rank web pages (absent when there is no embedder)."""

    embed: EmbedFn
    space: str
    cache: PageEmbeddingCache


def _clean_excerpt(excerpt: str) -> str:
    """Plain text of the draft excerpt: tags removed, entities decoded, short."""
    text = html.unescape(HTML_TAG.sub(" ", excerpt[: MAX_EXCERPT_CHARS * 4]))
    return " ".join(text.split())[:MAX_EXCERPT_CHARS]


def _query_text(title: str, excerpt: str) -> str:
    return f"{title}\n{excerpt}" if excerpt else title


def _fallback_part(web: WebContext, pages: list[WebPage], *, ok: bool) -> _Part:
    found = web.format_pages(pages)
    if not found.text:
        return _Part("", (), ok, degraded=True)
    return _Part("INTERNET SEARCH RESULTS\n" + found.text, tuple(found.hosts), ok, True)


def _ranked_part(
    web: WebContext, pages: list[WebPage], query: str, rankings: Sequence[_Ranking]
) -> _Part:
    """Rank with the first embedder that works; truncate only when none of them does."""
    for ranking in rankings:
        try:
            ranked = web_rank.rank_web(pages, query, ranking.embed, ranking.space, ranking.cache)
        except Exception as exc:  # noqa: BLE001 - a report must not fail because embedding failed
            log.warning("embedding failed (%s); trying the next embedder", type(exc).__name__)
            continue
        if not ranked:
            return _Part("", (), True)
        hosts = _unique(chunk.host for chunk in ranked if chunk.host)
        return _Part("INTERNET SEARCH RESULTS\n" + web_rank.format_ranked(ranked), hosts, True)
    log.warning("no embedder worked; web pages are truncated, not ranked")
    return _fallback_part(web, pages, ok=False)


def _web_part(
    web: WebContext | None, title: str, query: str, rankings: Sequence[_Ranking]
) -> _Part:
    if web is None:
        return _Part("", (), True)
    try:
        pages = web.fetch_pages(title)
    except Exception as exc:  # noqa: BLE001 - research is best-effort
        log.warning("web research failed (%s)", type(exc).__name__)  # never the title or message
        return _Part("", (), False)
    if not pages:
        return _Part("", (), True)
    if not rankings:
        return _fallback_part(web, pages, ok=True)
    return _ranked_part(web, pages, query, rankings)


def _local_lookup(
    rag: DocumentIndex, query: str, embed: EmbedFn | None
) -> tuple[str, tuple[str, ...]]:
    query_chunks = getattr(rag, "query_chunks", None)
    if query_chunks is None:  # minimal rag object exposing only query(): no structured sources
        return rag.query(query), ()
    chunks = query_chunks(query) if embed is None else query_chunks(query, embed=embed)
    return format_chunks(chunks), _unique(chunk.source for chunk in chunks)


def _rag_part(rag: DocumentIndex | None, query: str, embed: EmbedFn | None = None) -> _Part:
    if rag is None:
        return _Part("", (), True)
    try:
        local, sources = _local_lookup(rag, query, embed)
    except EmbedderError:
        log.warning("local document search skipped: the embedder is unavailable")
        return _Part("", (), False, degraded=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("local document lookup failed (%s)", type(exc).__name__)
        return _Part("", (), False)
    if not local:
        return _Part("", (), True)
    return _Part("LOCAL DOCUMENT EXCERPTS\n" + local, sources, True)


class _Provider:
    """One job's research lookups: ``provider(title, excerpt="") -> Research``."""

    def __init__(
        self,
        settings: Settings,
        rag: DocumentIndex | None,
        cache_dir: Path | None,
        embedder: Any | None,
        embed_timeout: float,
        fallback_embedder: Any | None = None,
    ):
        self._settings = settings
        self._embedder = embedder if embedder is not None else fallback_embedder
        self._web = WebContext(settings.web_results) if settings.use_web_search else None
        self._rag = rag if (rag and settings.use_rag) else None
        self._days = settings.research_cache_days
        self._root = cache_dir if self._days > 0 else None
        self._ranking = self._make_ranking(self._embedder, embed_timeout)
        # the built-in embedder also stands in when the main one is down: ranking beats truncating
        self._fallback = (
            self._make_ranking(fallback_embedder, embed_timeout)
            if embedder is not None and fallback_embedder is not None
            else None
        )
        self._local_embed = self._make_local_guard(embed_timeout)
        self._pruned = False
        self._warned = False
        self._version: str | None = ""
        self._version_read = False

    def _make_ranking(self, embedder: Any | None, timeout: float) -> _Ranking | None:
        if embedder is None:
            return None
        pages_dir = None if self._root is None else self._root / PAGE_CACHE_DIR
        space = str(getattr(embedder, "space", "") or getattr(embedder, "name", "?"))
        return _Ranking(
            BoundedEmbed(embedder, timeout), space, PageEmbeddingCache(pages_dir, self._days)
        )

    def _rankings(self) -> list[_Ranking]:
        return [r for r in (self._ranking, self._fallback) if r is not None]

    def _make_local_guard(self, timeout: float) -> EmbedFn | None:
        """The guarded embed for the query of the local documents, or ``None`` (not guarded).

        An embedder of the index's own vector space shares the web ranking's guard, so there is
        one deadline and one cool-down; the index's own embedder is guarded on its own.
        """
        if self._rag is None:
            return None
        if self._ranking is not None and getattr(self._rag, "space", None) == self._ranking.space:
            return self._ranking.embed
        own = getattr(self._rag, "embedder", None)
        return None if own is None else BoundedEmbed(own, timeout)

    def _current_version(self) -> str | None:
        """Read once per provider (one job), not once per finding."""
        if not self._version_read:
            self._version, self._version_read = index_version(self._rag), True
        return self._version

    def _cache_path(self, title: str, excerpt: str) -> Path | None:
        if self._root is None:
            return None
        indexed = self._current_version()
        if indexed is None:
            return None  # the index cannot be listed: the cache must not be trusted
        key = _cache_key(
            title,
            self._web is not None,
            self._rag is not None,
            self._settings.web_results,
            indexed,
            _ranking_fingerprint(self._embedder, excerpt),
        )
        return self._root / f"{key}.json"

    def _store(self, path: Path, research: Research) -> None:
        try:
            if not self._pruned:
                self._pruned = True  # lazily, once per provider
                prune_cache(path.parent, self._days)
            _write_cache(path, research)
        except OSError as exc:
            log.warning("could not write research cache entry: %s", exc)

    def _note_missing_embedder(self, part: _Part) -> None:
        if part.degraded and self._ranking is None and not self._warned:
            self._warned = True  # a permanent state: say it once, not per finding
            log.warning("no embedder available; web pages are truncated, not ranked")

    def __call__(self, title: str, excerpt: str = "") -> Research:
        if not title.strip():
            return Research()
        note = _clean_excerpt(excerpt)
        path = self._cache_path(title, note)
        if path is not None:
            cached = _read_cache(path, self._days)
            if cached is not None:
                return cached
        query = _query_text(title, note)
        parts = [
            _web_part(self._web, title, query, self._rankings()),
            _rag_part(self._rag, query, self._local_embed),
        ]
        self._note_missing_embedder(parts[0])
        sections = [part.section for part in parts if part.section]
        text = _neutralise("\n\n".join(sections))[:MAX_CONTEXT_CHARS]
        degraded = any(part.degraded for part in parts)
        research = Research(text, _unique(s for part in parts for s in part.sources), degraded)
        if path is not None and text and all(p.ok for p in parts):
            self._store(path, research)
        return research


def make_context_provider(
    settings: Settings,
    rag: DocumentIndex | None,
    cache_dir: Path | None = None,
    embedder: Any | None = None,
    *,
    embed_timeout: float = EMBED_TIMEOUT,
    fallback_embedder: Any | None = None,
) -> ContextProvider:
    """``provide(title, excerpt="") -> Research`` (see the module docstring for the data flow).

    ``embedder`` is the shared embedder (contract E1) used to rank web pages and to embed the
    query of the local documents. ``fallback_embedder`` (the built-in ONNX one) ranks the pages
    when there is no ``embedder`` or it fails; only when neither works are pages truncated
    (``Research.degraded``).
    """
    return _Provider(settings, rag, cache_dir, embedder, embed_timeout, fallback_embedder)
