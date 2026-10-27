"""Gaps between content: the empty, break-only and section-break paragraphs between two anchors.

An anchor is a block with content that is kept as it is: a paragraph with content, a table or a
content control. Empty paragraphs carrying a section break are gap members that are never
deleted (a next-page one is a fixed boundary, a continuous one only an empty line). A gap is
everything between two anchors (or between the start/end of the body and the nearest anchor).
Its page boundaries are
either fixed (they belong to an anchor or to a section break and are never removed) or held by
break-only paragraphs that tidy may delete.
"""

from __future__ import annotations

from dataclasses import dataclass

from .scan import BREAK, EMPTY, OTHER, PARA, SECTION, TABLE, Block, boundary_count


@dataclass(frozen=True)
class Gap:
    prev: Block | None  # anchor before the gap (None at the start of the body)
    next: Block | None  # anchor after the gap (None at the end of the body)
    members: tuple[Block, ...]

    @property
    def at_start(self) -> bool:
        return self.prev is None

    @property
    def at_end(self) -> bool:
        return self.next is None

    @property
    def edge(self) -> bool:
        return self.prev is None or self.next is None

    @property
    def breaks(self) -> tuple[Block, ...]:
        """Break-only paragraphs tidy may delete."""
        return tuple(m for m in self.members if m.kind == BREAK and m.removable)

    @property
    def empties(self) -> tuple[Block, ...]:
        """Every visibly empty paragraph (with or without a bookmark)."""
        return tuple(m for m in self.members if m.kind == EMPTY)

    @property
    def removable_empties(self) -> tuple[Block, ...]:
        return tuple(m for m in self.members if m.kind == EMPTY and m.removable)

    @property
    def fixed(self) -> int:
        """Page boundaries that stay whatever tidy does."""
        count = 0
        if self.prev is not None and self.prev.kind == PARA:
            count += self.prev.facts.trail + int(self.prev.page_after)
        if self.next is not None and self.next.kind == PARA:
            count += self.next.facts.lead + int(self.next.facts.pbb)
        for member in self.members:
            if member.kind == SECTION or not member.removable:
                count += boundary_count(member) + int(member.page_after)
        return count

    @property
    def boundaries(self) -> int:
        return self.fixed + sum(boundary_count(m) for m in self.breaks)

    @property
    def kept_paragraphs(self) -> int:
        """Member paragraphs tidy never deletes (section breaks, bookmark holders)."""
        return sum(1 for m in self.members if not m.removable)

    @property
    def needs_separator(self) -> bool:
        """A paragraph must stay: after a table at the end of the body, or between two tables."""
        tables = (TABLE, OTHER)
        prev_table = self.prev is not None and self.prev.kind in tables
        if self.at_end:
            return prev_table
        return prev_table and self.next is not None and self.next.kind in tables

    @property
    def after_heading(self) -> bool:
        return self.prev is not None and self.prev.is_heading


def is_anchor(block: Block) -> bool:
    return block.kind in (PARA, TABLE, OTHER)


class Gaps(tuple[Gap, ...]):
    """The gaps of a body, in order, with a lookup by the anchor before / after a gap."""

    def _index(self, side: str) -> dict[int, Gap]:
        found = self.__dict__.setdefault("_by", {})
        if side not in found:
            found[side] = {
                id(anchor): gap
                for gap in reversed(self)  # the first gap of an anchor wins, as a scan would
                if (anchor := getattr(gap, side)) is not None
            }
        return found[side]

    def after(self, block: Block) -> Gap | None:
        return self._index("prev").get(id(block))

    def before(self, block: Block) -> Gap | None:
        return self._index("next").get(id(block))


def build_gaps(blocks: tuple[Block, ...] | list[Block]) -> Gaps:
    """Every gap of the body in order, including empty ones (no members)."""
    gaps: list[Gap] = []
    prev: Block | None = None
    members: list[Block] = []
    for block in blocks:
        if is_anchor(block):
            gaps.append(Gap(prev, block, tuple(members)))
            prev, members = block, []
        else:
            members.append(block)
    gaps.append(Gap(prev, None, tuple(members)))
    return Gaps(gaps)


def gap_after(gaps: tuple[Gap, ...], block: Block) -> Gap | None:
    if isinstance(gaps, Gaps):
        return gaps.after(block)
    return next((g for g in gaps if g.prev is block), None)


def gap_before(gaps: tuple[Gap, ...], block: Block) -> Gap | None:
    if isinstance(gaps, Gaps):
        return gaps.before(block)
    return next((g for g in gaps if g.next is block), None)
