"""What tidy does with one gap: which empty and break paragraphs go, and where a break moves.

Rules (each gap separately):
* at the start or end of the body every removable page break goes;
* between two anchors at most one page boundary stays: none of the removable breaks when a fixed
  boundary exists, else one. It becomes pageBreakBefore on the next paragraph, or, before a table
  or a content control, the last break paragraph is kept (with exactly one break);
* empty paragraphs: none next to a page boundary, at the start or end of the body, or right after a
  heading; otherwise at most one. A paragraph that Word requires (after a final table, between
  two tables) always stays.
"""

from __future__ import annotations

from dataclasses import dataclass

from .groups import Gap
from .model import TidyOptions
from .scan import EMPTY, PARA, Block, boundary_count


@dataclass(frozen=True)
class GapPlan:
    remove: tuple[Block, ...] = ()  # paragraphs to delete
    pbb_on: Block | None = None  # paragraph that gets pageBreakBefore (a converted break)
    normalize: Block | None = None  # kept break paragraph reduced to exactly one boundary
    strip: Block | None = None  # kept break paragraph that loses its breaks (a separator)

    @property
    def changes(self) -> bool:
        return bool(self.remove) or any(
            item is not None for item in (self.pbb_on, self.normalize, self.strip)
        )

    @property
    def empties_removed(self) -> int:
        return sum(1 for block in self.remove if block.kind == EMPTY)

    @property
    def breaks_removed(self) -> int:
        removed = sum(boundary_count(b) for b in self.remove if b.kind != EMPTY)
        if self.normalize is not None:
            removed += boundary_count(self.normalize) - 1
        if self.strip is not None:
            removed += boundary_count(self.strip)
        return removed - (1 if self.pbb_on is not None else 0)

    @property
    def converted(self) -> int:
        return 1 if self.pbb_on is not None else 0


@dataclass(frozen=True)
class _Breaks:
    remove: tuple[Block, ...] = ()
    keep: Block | None = None
    pbb_on: Block | None = None


def _plan_breaks(gap: Gap) -> _Breaks:
    breaks = gap.breaks
    if not breaks:
        return _Breaks()
    if gap.edge or gap.fixed > 0:
        return _Breaks(remove=breaks)
    if gap.next is not None and gap.next.kind == PARA:
        return _Breaks(remove=breaks, pbb_on=gap.next)
    return _Breaks(remove=breaks[:-1], keep=breaks[-1])


def _allowed_empties(gap: Gap, boundary_left: bool, kept_break: bool) -> int:
    if gap.at_end:
        allowed = 1 if gap.needs_separator and not (gap.kept_paragraphs or kept_break) else 0
    elif gap.at_start or boundary_left or gap.after_heading:
        allowed = 0
    else:
        allowed = 1
    bookmark_holders = sum(1 for m in gap.members if m.kind == EMPTY and not m.removable)
    return max(0, allowed - bookmark_holders)


def plan_gap(gap: Gap, options: TidyOptions) -> GapPlan:
    if gap.at_start and gap.at_end:
        return GapPlan()  # a body without content: Word needs its paragraphs as they are
    breaks = _plan_breaks(gap) if options.fix_page_breaks else _Breaks()
    if options.fix_page_breaks:
        boundary_left = gap.fixed > 0 or breaks.keep is not None or breaks.pbb_on is not None
    else:
        boundary_left = gap.boundaries > 0
    empties: tuple[Block, ...] = ()
    kept_empties = len(gap.removable_empties)
    if options.collapse_empty_runs:
        allowed = _allowed_empties(gap, boundary_left, breaks.keep is not None)
        empties = gap.removable_empties[allowed:]
        kept_empties = min(allowed, len(gap.removable_empties))
    removed = [*breaks.remove, *empties]
    strip = None
    lonely = gap.at_end and gap.needs_separator and not gap.kept_paragraphs
    if lonely and breaks.remove and not kept_empties and breaks.keep is None:
        strip = breaks.remove[-1]
        removed.remove(strip)
    normalize = breaks.keep if breaks.keep and boundary_count(breaks.keep) > 1 else None
    gone = {id(r) for r in removed}
    ordered = tuple(m for m in gap.members if id(m) in gone)
    return GapPlan(ordered, breaks.pbb_on, normalize, strip)
