"""One analysed document: parsed parts, field scan, referenced bookmarks, blocks, segments."""

from __future__ import annotations

import re
from dataclasses import dataclass

from pwndoc_ai.docx_engine import limits
from pwndoc_ai.docx_engine.errors import DocxError

from . import budget
from .fields import FieldScan, scan_fields
from .load import MAX_EXTRA_PARTS, LoadedDoc
from .ooxml import w
from .scan import Block, Segment, build_blocks, segments

EXTERNAL_KINDS = ("header", "footer", "footnotes", "endnotes", "comments")
ALL_REFERENCED = "\x00all"
MAX_EXTERNAL_BYTES = 16 * 1024 * 1024  # more side-part text than this: every bookmark is kept
SUBSTRING_LIMIT = 1024 * 1024  # up to this much text a name is searched as a substring
WORD = re.compile(r"\w+")


@dataclass(frozen=True)
class Analysis:
    doc: LoadedDoc
    fields: FieldScan
    referenced: frozenset[str]  # bookmark names used anywhere (body fields, TOC, other parts)
    external: frozenset[str]  # bookmark names that headers, footers, notes or comments mention
    blocks: tuple[Block, ...]
    segments: tuple[Segment, ...]


def _read_external(doc: LoadedDoc) -> str:
    chunks: list[str] = []
    total = 0
    for kind in EXTERNAL_KINDS:
        for name in doc.parts.get(kind, ())[:MAX_EXTRA_PARTS]:
            try:
                payload = doc.package.read(name, limits.MAX_SMALL_XML_BYTES)
            except DocxError:
                # An unreadable side part: treat every bookmark as referenced (never remove one).
                return ALL_REFERENCED
            total += len(payload)
            if total > MAX_EXTERNAL_BYTES:
                return ALL_REFERENCED
            chunks.append(payload.decode("utf-8", "replace"))
    return "\n".join(chunks).lower()


class _External:
    """The text of headers, footers, notes and comments, read once per document, and which
    bookmark names it mentions (answers cached: the parts do not change while tidy runs)."""

    def __init__(self, doc: LoadedDoc) -> None:
        self.text = _read_external(doc)
        self.everything = self.text == ALL_REFERENCED
        self.words: frozenset[str] | None = None
        self.answers: dict[str, bool] = {}

    def mentions(self, name: str) -> bool:
        if self.everything:
            return True
        if name not in self.answers:
            self.answers[name] = self._lookup(name.lower())
        return self.answers[name]

    def _lookup(self, lowered: str) -> bool:
        if not self.text:
            return False
        if len(self.text) <= SUBSTRING_LIMIT:
            return lowered in self.text
        if not WORD.fullmatch(lowered):
            return True  # cannot be looked up as a word in a big text: keep the bookmark
        if self.words is None:
            self.words = frozenset(WORD.findall(self.text))
        return lowered in self.words


def _external(doc: LoadedDoc) -> _External:
    found = doc.cache.get("external")
    if not isinstance(found, _External):
        found = _External(doc)
        doc.cache["external"] = found
    return found


def _references(doc: LoadedDoc, scan: FieldScan) -> tuple[frozenset[str], frozenset[str]]:
    names = {n for n in (m.get(w("name")) for m in doc.body.iter(w("bookmarkStart"))) if n}
    external: set[str] = set()
    if names:
        outside = _external(doc)
        external = {n for n in names if outside.mentions(n)}
    used = {n for n in names if scan.referenced(n)} | external
    return frozenset(used), frozenset(external)


def analyze(doc: LoadedDoc) -> Analysis:
    """Analyse the current state of ``doc.root`` (call again after an edit)."""
    budget.check()
    scan = scan_fields(doc.body)
    referenced, external = _references(doc, scan)
    blocks = build_blocks(doc, referenced)
    return Analysis(doc, scan, referenced, external, tuple(blocks), tuple(segments(blocks)))
