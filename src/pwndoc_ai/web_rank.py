"""Chunk fetched web pages, embed the chunks and keep the passages closest to the finding.

Instead of pasting the first N characters of each search result, a page is cut into chunks of
about ``CHUNK_CHARS`` characters (``CHUNK_OVERLAP`` overlap, split at word boundaries), the chunks
are embedded, and the chunks most similar to the query (the finding title plus a short excerpt
of the draft) are returned best first, each tagged with the host it came from.

Bounds (nothing here can grow with the size of the internet):

* at most ``MAX_CHUNKS_PER_PAGE`` chunks of one page and ``MAX_CHUNKS_PER_FINDING`` chunks of
  all pages of one finding are embedded (the budget is shared evenly between the pages), i.e.
  at most 60 chunk embeddings plus one query embedding per finding, and none for a page whose
  vectors are already in the :class:`~pwndoc_ai.page_cache.PageEmbeddingCache`;
* the returned text is at most ``WEB_BUDGET_CHARS`` characters;
* a chunk that repeats another one (same words, or cosine above ``NEAR_DUPLICATE``) is dropped.

``embed`` is any ``Sequence[str] -> list[list[float]]``; it raises
:class:`~pwndoc_ai.embedder.EmbedderError` when it cannot embed, and so does this module when
the vectors it gets back are unusable. The caller decides what to do then.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import NamedTuple

from .embedder import EmbedderError
from .page_cache import EmbedFn, PageEmbeddingCache, page_key
from .web_context import TRUNCATION_MARKER, WebPage

CHUNK_CHARS = 800
CHUNK_OVERLAP = 120
MAX_CHUNKS_PER_PAGE = 20
MAX_CHUNKS_PER_FINDING = 60
WEB_BUDGET_CHARS = 6000
NEAR_DUPLICATE = 0.95
MAX_QUERY_CHARS = 1000
WORD = re.compile(r"\w+", re.UNICODE)


class RankedChunk(NamedTuple):
    host: str  # where the text came from (after redirects)
    text: str
    score: float
    via: str = ""  # the host of the search result, when it redirected somewhere else


def normalise(text: str) -> str:
    """Words only, lower case: the identity of a chunk for duplicate detection."""
    return " ".join(WORD.findall(text.lower()))


def unique_chunks(chunks: Sequence[str]) -> list[str]:
    """``chunks`` without repeats (same words), first occurrence kept."""
    seen: set[str] = set()
    kept: list[str] = []
    for chunk in chunks:
        identity = normalise(chunk)
        if identity and identity not in seen:
            seen.add(identity)
            kept.append(chunk)
    return kept


def _window_end(text: str, start: int) -> int:
    end = min(start + CHUNK_CHARS, len(text))
    if end == len(text):
        return end
    cut = text.rfind(" ", start + CHUNK_CHARS // 2, end)
    return cut if cut > start else end


def _next_start(text: str, start: int, end: int) -> int:
    """Where the next chunk begins: ``CHUNK_OVERLAP`` characters back, at a word start."""
    gap = text.find(" ", max(start + 1, end - CHUNK_OVERLAP), end)
    return gap + 1 if gap != -1 else end


def chunk_page(text: str, max_chunks: int = MAX_CHUNKS_PER_PAGE) -> list[str]:
    """Overlapping chunks of ``text`` (at most ``max_chunks`` distinct ones, in page order)."""
    clean = " ".join(text.removesuffix(TRUNCATION_MARKER).split())
    chunks: list[str] = []
    seen: set[str] = set()
    start = 0
    while start < len(clean) and len(chunks) < max_chunks:
        end = _window_end(clean, start)
        piece = clean[start:end].strip()
        identity = normalise(piece)
        if identity and identity not in seen:
            seen.add(identity)
            chunks.append(piece)
        start = len(clean) if end == len(clean) else _next_start(clean, start, end)
    return chunks


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity; ``0.0`` when a vector is all zeros."""
    if len(a) != len(b):
        raise EmbedderError("the embedder returned vectors of different sizes")
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / norm if norm else 0.0


def _checked(embed: EmbedFn, texts: Sequence[str]) -> list[list[float]]:
    """``embed(texts)`` with the answer validated: one finite vector per text."""
    vectors = embed(list(texts))
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise EmbedderError("the embedder returned the wrong number of vectors")
    for vector in vectors:
        if not vector or not all(math.isfinite(x) for x in vector):
            raise EmbedderError("the embedder returned an unusable vector")
    return vectors


def _share(page_count: int) -> int:
    """Chunks one page may contribute: the finding's budget shared between its pages."""
    return max(1, min(MAX_CHUNKS_PER_PAGE, MAX_CHUNKS_PER_FINDING // max(page_count, 1)))


def _select(
    query: Sequence[float], candidates: Sequence[tuple[str, str, str, list[float]]]
) -> list[RankedChunk]:
    scored = sorted(
        ((cosine(query, vec), host, via, text, vec) for host, via, text, vec in candidates),
        key=lambda item: -item[0],
    )
    chosen: list[RankedChunk] = []
    chosen_vectors: list[list[float]] = []
    seen: set[str] = set()
    used = 0
    for score, host, via, text, vec in scored:
        identity = normalise(text)
        if identity in seen or used + len(text) > WEB_BUDGET_CHARS:
            continue
        if any(cosine(vec, other) >= NEAR_DUPLICATE for other in chosen_vectors):
            continue
        seen.add(identity)
        chosen.append(RankedChunk(host, text, score, via))
        chosen_vectors.append(vec)
        used += len(text)
    return chosen


def rank_web(
    pages: Sequence[WebPage],
    query: str,
    embed: EmbedFn,
    space: str,
    cache: PageEmbeddingCache,
) -> list[RankedChunk]:
    """The passages of ``pages`` most similar to ``query``, best first, within the budget."""
    query = " ".join(query.split())[:MAX_QUERY_CHARS]
    share = _share(len(pages))
    plan = [(page, chunk_page(page.text, share)) for page in pages]
    plan = [(page, chunks) for page, chunks in plan if chunks]
    if not plan or not query:
        return []
    (query_vector,) = _checked(embed, [query])
    candidates: list[tuple[str, str, str, list[float]]] = []
    for page, chunks in plan:
        key = page_key(page.text, space, (CHUNK_CHARS, CHUNK_OVERLAP))
        vectors = cache.embed_page(
            key, chunks, lambda texts: _checked(embed, texts), width=len(query_vector)
        )
        candidates.extend(
            (page.host, page.via, text, vec) for text, vec in zip(chunks, vectors, strict=True)
        )
    return _select(query_vector, candidates)


def format_ranked(chunks: Sequence[RankedChunk]) -> str:
    """Chunks as ``[web: host]`` labelled paragraphs, in rank order."""
    return "\n\n".join(f"[web: {_label(chunk)}]\n{chunk.text}" for chunk in chunks)


def _label(chunk: RankedChunk) -> str:
    """The host a passage came from, and the search-result host that redirected to it."""
    host = chunk.host or "unknown"
    return f"{host} (via {chunk.via})" if chunk.via else host
