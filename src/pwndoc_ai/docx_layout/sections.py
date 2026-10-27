"""Heading sections: empty sections tidy may remove, and headings at risk of being orphaned.

The section of a heading runs to the next heading of the same or a higher level. It is empty when
it holds only empty or break-only paragraphs (and empty sub-sections). Only a plain heading is
ever removed: never the first heading, never one with an object, a field, a page or section
break, a direct page-break-before, or a bookmark that something other than a removable table of
contents entry refers to.
"""

from __future__ import annotations

from dataclasses import dataclass

from lxml import etree

from .context import Analysis
from .groups import Gap, gap_after, is_anchor
from .scan import BREAK, EMPTY, PARA, TABLE, Block

MAX_WHERE = 40


@dataclass(frozen=True)
class EmptySection:
    heading: Block
    nested: tuple[Block, ...]  # empty sub-section headings removed with it
    empties: tuple[Block, ...]  # removable empty paragraphs of the section
    toc_entries: tuple[etree._Element, ...]  # table of contents paragraphs removed with it

    @property
    def headings(self) -> tuple[Block, ...]:
        return (self.heading, *self.nested)


@dataclass(frozen=True)
class EmptySections:
    sections: tuple[EmptySection, ...]  # outermost removable sections, document order

    @property
    def headings(self) -> tuple[Block, ...]:
        return tuple(h for section in self.sections for h in section.headings)


@dataclass(frozen=True)
class Orphan:
    block: Block
    missing_keep: bool  # no keepNext although something follows
    break_after: bool  # a page boundary separates the heading from its own content
    movable: bool  # that boundary is held only by break paragraphs tidy may delete

    @property
    def fixable(self) -> bool:
        return self.missing_keep or self.movable


def where_text(block: Block) -> str:
    text = " ".join(block.facts.text.split())
    return text[:MAX_WHERE] if text else f"paragraph {block.ordinal}"


def _toc_entries(block: Block, analysis: Analysis) -> tuple[etree._Element, ...] | None:
    """The TOC paragraphs to remove with ``block``; None when a bookmark must stay."""
    names = set(block.facts.bookmarks)
    entries: list[etree._Element] = []
    for name in names:
        if name in analysis.fields.outside_refs or name in analysis.external:
            return None
        for paragraph in analysis.fields.toc_refs.get(name, ()):
            refs = analysis.fields.toc_removable.get(paragraph)
            if refs is None or not refs <= names:
                return None
            if paragraph not in entries:
                entries.append(paragraph)
    return tuple(entries)


def _plain(block: Block) -> bool:
    facts = block.facts
    return not (facts.rich or facts.brs or facts.pbb_direct or facts.unpaired) and (
        facts.sect is None and not block.page_after
    )


def section_end(blocks: tuple[Block, ...], index: int) -> int:
    level = blocks[index].facts.heading or 0
    for later in range(index + 1, len(blocks)):
        other = blocks[later]
        if other.is_heading and (other.facts.heading or 0) <= level:
            return later
    return len(blocks)


def boundaries_ok(blocks: tuple[Block, ...], index: int, end: int) -> bool:
    """Removing blocks[index:end] leaves no blank page: at most one fixed page boundary
    between the surrounding content, none at the end of the body."""
    start = index
    while start > 0 and not is_anchor(blocks[start - 1]):
        start -= 1
    prev = blocks[start - 1] if start > 0 else None
    following = blocks[end] if end < len(blocks) else None
    gap = Gap(prev, following, tuple(b for b in blocks[start:end] if not b.is_heading))
    return gap.fixed == 0 if gap.edge else gap.fixed <= 1


def _candidates(analysis: Analysis) -> dict[int, tuple[etree._Element, ...]]:
    """Headings (block index) whose section is empty and that may go, with their TOC entries."""
    blocks = analysis.blocks
    headings = [i for i, block in enumerate(blocks) if block.is_heading]
    removable: dict[int, tuple[etree._Element, ...]] = {}
    for index in reversed(headings[1:]):
        inner = range(index + 1, section_end(blocks, index))
        empty = all(blocks[j].kind in (EMPTY, BREAK) or j in removable for j in inner)
        entries = _toc_entries(blocks[index], analysis) if empty and _plain(blocks[index]) else None
        if entries is not None:
            removable[index] = entries
    return removable


def _section(
    blocks: tuple[Block, ...], index: int, found: dict[int, tuple[etree._Element, ...]]
) -> EmptySection:
    inner = blocks[index + 1 : section_end(blocks, index)]
    nested = tuple(b for b in inner if b.is_heading)
    entries = [e for i in range(index, index + 1 + len(inner)) for e in found.get(i, ())]
    empties = tuple(b for b in inner if b.kind == EMPTY and b.removable)
    return EmptySection(blocks[index], nested, empties, tuple(dict.fromkeys(entries)))


def empty_sections(analysis: Analysis) -> EmptySections:
    blocks = analysis.blocks
    found = _candidates(analysis)
    outer: list[EmptySection] = []
    covered = -1
    for index in sorted(found):
        end = section_end(blocks, index)
        if index < covered or not boundaries_ok(blocks, index, end):
            continue
        outer.append(_section(blocks, index, found))
        covered = end
    return EmptySections(tuple(outer))


def _own_content(heading: Block, gap: Gap) -> bool:
    following = gap.next
    if following is None:
        return False
    return not (
        following.is_heading and (following.facts.heading or 0) <= (heading.facts.heading or 0)
    )


def _is_figure(block: Block) -> bool:
    return block.kind == TABLE or (
        block.kind == PARA and "O" in block.facts.tokens and "T" not in block.facts.tokens
    )


def _heading_orphan(block: Block, gaps: tuple[Gap, ...], has_next: bool) -> Orphan | None:
    gap = gap_after(gaps, block)
    missing = not block.facts.keep_next and has_next
    broken = gap is not None and gap.boundaries > 0 and _own_content(block, gap)
    movable = broken and gap is not None and gap.fixed == 0 and bool(gap.breaks)
    if missing or broken:
        return Orphan(block, missing, broken, movable)
    return None


def orphans(analysis: Analysis, gaps: tuple[Gap, ...]) -> tuple[Orphan, ...]:
    """Headings (and captions above a table or picture) that can end up alone on a page."""
    blocks = analysis.blocks
    found: list[Orphan] = []
    for index, block in enumerate(blocks):
        following = blocks[index + 1] if index + 1 < len(blocks) else None
        if block.is_heading:
            orphan = _heading_orphan(block, gaps, following is not None)
            if orphan is not None:
                found.append(orphan)
        elif block.kind == PARA and block.facts.caption and not block.facts.keep_next:
            if following is not None and _is_figure(following):
                found.append(Orphan(block, True, False, False))
    return tuple(found)
