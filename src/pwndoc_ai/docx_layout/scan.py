"""Body blocks and page-break semantics.

The body is a sequence of blocks. Between blocks there can be page boundaries: a hard break
(``w:br type=page``), ``pageBreakBefore``, or a section break whose next section starts on a
new page. A segment is the content between two boundaries (a page, ignoring natural overflow);
a segment with no content is a blank page.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lxml import etree

from . import budget
from .load import LoadedDoc
from .ooxml import local, w
from .paragraphs import ParaFacts, classify

NEW_PAGE_TYPES = frozenset({"nextPage", "evenPage", "oddPage"})
CHECK_EVERY = 1024  # body children between two looks at the time budget

# Block kinds
EMPTY = "E"  # visibly empty paragraph
BREAK = "B"  # empty paragraph that only holds a page break
SECTION = "S"  # empty paragraph that carries a section break (never touched)
PARA = "P"  # paragraph with content (may end a section)
TABLE = "T"
OTHER = "O"  # content control and other block-level content


@dataclass(frozen=True)
class Block:
    pos: int  # index among the body children
    kind: str
    element: etree._Element = field(compare=False, repr=False)
    ordinal: int = 0  # 1-based position among body paragraphs, 0 for non paragraphs
    facts: ParaFacts = ParaFacts()
    page_after: bool = False  # a section break paragraph whose next section starts a new page

    @property
    def is_empty_like(self) -> bool:
        return self.kind in (EMPTY, BREAK, SECTION)

    @property
    def removable(self) -> bool:
        """An empty or break-only paragraph that tidy may delete (it holds no bookmark)."""
        return self.kind in (EMPTY, BREAK) and not self.facts.bookmarks

    @property
    def is_heading(self) -> bool:
        return self.kind == PARA and self.facts.heading is not None


@dataclass(frozen=True)
class Boundary:
    block: Block | None  # the block that causes the break
    origin: str  # "break" | "pbb" | "section"

    @property
    def removable(self) -> bool:
        """A pure break paragraph can be deleted (or trimmed to one break)."""
        return self.block is not None and self.block.kind == BREAK and self.block.removable


@dataclass(frozen=True)
class Segment:
    start: Boundary | None
    end: Boundary | None
    blank: bool
    first: bool
    last: bool
    blocks: tuple[Block, ...]


def boundary_count(block: Block) -> int:
    """Page boundaries one paragraph causes by itself (breaks plus pageBreakBefore)."""
    return block.facts.brs + (1 if block.facts.pbb else 0)


def _kind_of(facts: ParaFacts) -> str:
    if facts.sect is not None:
        return PARA if facts.visible else SECTION
    if facts.visible:
        return PARA
    return BREAK if (facts.brs or facts.pbb) else EMPTY


def _next_page(sect: etree._Element | None) -> bool:
    if sect is None:
        return False
    kind = sect.find(w("type"))
    return (kind.get(w("val")) if kind is not None else "nextPage") in NEW_PAGE_TYPES


def build_blocks(doc: LoadedDoc, referenced: frozenset[str] | set[str]) -> list[Block]:
    """Blocks of the body in order. Paragraphs inside tables and content controls are not blocks."""
    entries: list[tuple[int, str, etree._Element, ParaFacts]] = []
    for pos, child in enumerate(doc.body):
        if pos % CHECK_EVERY == 0:
            budget.check()
        name = local(child)
        if name == "p":
            facts = classify(child, doc.styles, referenced)
            entries.append((pos, _kind_of(facts), child, facts))
        elif name == "tbl":
            entries.append((pos, TABLE, child, ParaFacts()))
        elif name in ("sdt", "customXml", "altChunk"):
            entries.append((pos, OTHER, child, ParaFacts()))
    final = doc.body.find(w("sectPr"))
    sects = [f.sect for _, _, _, f in entries if f.sect is not None] + [final]
    blocks: list[Block] = []
    ordinal = 0
    sect_index = 0
    for pos, kind, element, facts in entries:
        is_para = local(element) == "p"
        ordinal += 1 if is_para else 0
        page_after = False
        if facts.sect is not None:
            page_after = _next_page(sects[sect_index + 1])
            sect_index += 1
        blocks.append(Block(pos, kind, element, ordinal if is_para else 0, facts, page_after))
    return blocks


def _events(block: Block) -> list[tuple[str, Boundary | None]]:
    """Flow events of a block: ("C", None) content, ("E", None) blank line, ("X", boundary)."""
    facts = block.facts
    events: list[tuple[str, Boundary | None]] = []
    if block.kind in (TABLE, OTHER):
        return [("C", None)]
    if facts.pbb:
        events.append(("P", Boundary(block, "pbb")))
    if block.kind in (EMPTY, BREAK, SECTION):
        events += [("X", Boundary(block, "break"))] * facts.brs
        events.append(("E", None))
    else:
        events += [("X", Boundary(block, "break"))] * facts.lead
        events.append(("C", None))
        if facts.inner:
            events += [("X", Boundary(block, "break"))] * facts.inner
            events.append(("C", None))
        if facts.trail:
            events += [("X", Boundary(block, "break"))] * facts.trail
            events.append(("E", None))
    if block.page_after:
        events.append(("X", Boundary(block, "section")))
    return events


def segments(blocks: list[Block]) -> list[Segment]:
    """Split the flow at page boundaries. A pageBreakBefore at the top of a page is a no-op."""
    result: list[Segment] = []
    start: Boundary | None = None
    tokens: list[str] = []
    members: list[Block] = []

    def close(end: Boundary | None, last: bool) -> None:
        nonlocal start, tokens, members
        result.append(Segment(start, end, "C" not in tokens, not result, last, tuple(members)))
        start, tokens, members = end, [], []

    for block in blocks:
        for kind, boundary in _events(block):
            if kind == "P" and not tokens:
                continue  # pageBreakBefore where a page already starts
            if kind in ("X", "P"):
                close(boundary, False)
            else:
                tokens.append(kind)
        members.append(block)
    close(None, True)
    return result
