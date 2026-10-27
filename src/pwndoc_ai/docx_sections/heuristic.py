"""Deterministic block typing: the fallback of the AI and the repair of its coverage.

Pure and total: every block gets exactly one label. Rules in order (EXPORT_DESIGN.md (h)):
style names, then the outline level, then size/bold clustering for unstyled documents (the PDF
rebuild rules), then mono font -> code, caption and TOC patterns, tables, drawings, lists, body.
Labels from a style name or an outline level carry ``source="style"``, the rest ``heuristic``.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence

from ..block_types import heading_type
from .model import Block, BlockLabel

HEADING_RATIO = 1.12
BOLD_RATIO = 0.95
TITLE_RATIO = 1.5
MAX_HEADING_CHARS = 140
MAX_BOLD_HEADING_CHARS = 90
MAX_HEADING_SHARE = 0.25
MAX_LEVELS = 4
TITLE_WINDOW = 12
CAPS_MAX_CHARS = 60

CAPTION = re.compile(r"^(figure|fig\.|table|listing)\s*\d+", re.IGNORECASE)
TOC_LINE = re.compile(r"\.{4,}\s*\d+$")
BULLET = re.compile(r"^([•▪◦·–\-*o]\s+|(\d{1,3}|[a-z]|[ivx]{1,5})[.)]\s+)", re.IGNORECASE)
NUMBERED_HEADING = re.compile(r"^\d{1,2}(\.\d{1,2}){0,3}\.?\s+\S")
PLAIN_STYLES = ("", "normal", "body text", "default", "standard", "text body", "no spacing")

STYLE_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"^(toc|contents)\b|^table of contents"), "toc"),
    (re.compile(r"^title$"), "title"),
    (re.compile(r"^caption$|^table of figures"), "caption"),
    (re.compile(r"quote"), "quote"),
    (re.compile(r"code|source|preformatted|verbatim|console"), "code"),
    (re.compile(r"^footnote"), "footnote"),
    (re.compile(r"^header$"), "header"),
    (re.compile(r"^footer$"), "footer"),
    (re.compile(r"^list|bullet"), "list"),
)


def _style_type(block: Block) -> str | None:
    name = block.style.strip().lower()
    for pattern, kind in STYLE_RULES:
        if pattern.search(name):
            return kind
    match = re.fullmatch(r"heading\s*([1-9])", name)
    if match:
        return heading_type(int(match.group(1)))
    if block.kind == "paragraph" and block.level:
        return heading_type(block.level)
    return None


def _is_plain(block: Block) -> bool:
    return block.kind == "paragraph" and block.style.strip().lower() in PLAIN_STYLES


def body_size(blocks: Sequence[Block]) -> float | None:
    """The size carrying the most characters among plain paragraphs (rounded to 0.5 pt)."""
    weights: Counter = Counter()
    for block in blocks:
        if _is_plain(block) and block.size:
            weights[round(block.size * 2) / 2] += block.length
    return weights.most_common(1)[0][0] if weights else None


def _candidate(block: Block, body: float) -> bool:
    text = block.text.strip()
    if not text or block.mono or not block.size or len(text) > MAX_HEADING_CHARS:
        return False
    if text.endswith((",", ";")) or TOC_LINE.search(text) or CAPTION.match(text):
        return False
    if block.size >= HEADING_RATIO * body:
        return True
    short = len(text) <= MAX_BOLD_HEADING_CHARS and not text.endswith(".")
    return block.bold and block.size >= BOLD_RATIO * body and short


def _cluster(candidates: Sequence[Block]) -> dict[str, str]:
    keys = sorted({(c.size or 0.0, c.bold) for c in candidates}, reverse=True)
    rank = {key: min(index, MAX_LEVELS - 1) + 1 for index, key in enumerate(keys)}
    return {c.id: heading_type(rank[(c.size or 0.0, c.bold)]) for c in candidates}


def _title_id(blocks: Sequence[Block], body: float) -> str | None:
    """A cover title: an early, short, plain paragraph at least 1.5 x body, its size unique."""
    sizes = Counter(block.size for block in blocks if block.text)
    for block in blocks[:TITLE_WINDOW]:
        big = block.size is not None and block.size >= TITLE_RATIO * body
        if _is_plain(block) and block.text and big and sizes[block.size] <= 2:
            return block.id
    return None


def size_headings(blocks: Sequence[Block], styled: bool) -> dict[str, str]:
    """Headings from size/bold clustering (only the title when the document has heading
    styles). Empty when too many lines qualify (a form or a poster, not a report)."""
    body = body_size(blocks)
    if body is None:
        return {}
    found: dict[str, str] = {}
    title = _title_id(blocks, body)
    if not styled:
        plain = [block for block in blocks if _is_plain(block) and block.text]
        candidates = [b for b in plain if b.id != title and _candidate(b, body)]
        if candidates and len(candidates) <= MAX_HEADING_SHARE * max(len(plain), 1):
            found = _cluster(candidates)
    if title is not None:
        found = {**found, title: "title"}
    return found


def _pattern_type(block: Block, figures: Iterable[str]) -> str:
    text = block.text.strip()
    if block.kind == "table":
        return "table"
    if not text:
        return "figure" if block.id in figures else "body"
    if block.mono:
        return "code"
    if CAPTION.match(text):
        return "caption"
    if TOC_LINE.search(text):
        return "toc"
    if block.list or BULLET.match(text):
        return "list"
    return "body"


def _caps_heading(block: Block) -> bool:
    text = block.text.strip()
    letters = [char for char in text if char.isalpha()]
    return (
        _is_plain(block)
        and 3 <= len(letters)
        and len(text) <= CAPS_MAX_CHARS
        and text.isupper()
        and block.bold
        and not text.endswith(".")
    )


def heuristic_labels(
    blocks: Sequence[Block], figures: Iterable[str] = ()
) -> tuple[BlockLabel, ...]:
    """One label per block, in block order."""
    figure_ids = frozenset(figures)
    styled_types = {block.id: _style_type(block) for block in blocks}
    styled = any(kind and kind.startswith("heading") for kind in styled_types.values())
    sized = size_headings(blocks, styled)
    labels = []
    for block in blocks:
        kind = styled_types[block.id]
        if kind is not None and block.kind == "paragraph" and block.text.strip():
            labels.append(BlockLabel(block.id, kind, "style"))
            continue
        kind = sized.get(block.id) or _pattern_type(block, figure_ids)
        if kind == "body" and _caps_heading(block) and not styled:
            kind = "heading_2"
        if kind == "body" and not styled and NUMBERED_HEADING.match(block.text) and block.bold:
            kind = heading_type(min(block.text.split()[0].rstrip(".").count(".") + 1, 4))
        labels.append(BlockLabel(block.id, kind, "heuristic"))
    return tuple(labels)
