"""Vertical placement: turn measured PDF baselines into paragraph spacing.

Word and LibreOffice put the baseline of an exact-height line at 80 % of the line height, so a
paragraph whose first baseline must land at ``b`` with line height ``L`` starts at
``b - 0.8 * L``. ``Flow`` keeps the bottom of the last placed block (page coordinates in pt)
and returns the ``before`` spacing for the next paragraph.
"""

from __future__ import annotations

from dataclasses import dataclass

from lxml import etree
from ox import Para, el, tw

BASELINE_RATIO = 0.8


@dataclass
class Flow:
    y: float

    def place(self, baseline: float, line: float, lines: int = 1, after: float = 0) -> float:
        """Spacing-before so the first baseline lands at ``baseline``; advances the cursor."""
        top = baseline - BASELINE_RATIO * line
        before = round(max(0.0, top - self.y), 2)
        self.y = max(self.y, top) + lines * line + after
        return before

    def fmt(self, base: Para, baseline: float, lines: int = 1, after: float = 0) -> Para:
        line = base.line or style_line(base.style)
        return base.with_(
            before=self.place(baseline, line, lines, after), line=line, after=after or None
        )

    def block(self, top: float, height: float) -> float:
        """A table (or other block) occupying ``top``..``top+height``; returns the gap above."""
        gap = round(max(0.0, top - self.y), 2)
        self.y = top + height
        return gap


DEFAULT_LINE = 12.0


def style_line(style: str | None) -> float:
    """Exact line height defined by paragraph style ``style`` (DEFAULT_LINE when none)."""
    from pd_styles import all_styles  # local import: pd_styles does not depend on pd_flow

    for sdef in all_styles():
        if sdef.sid == style and sdef.para is not None and sdef.para.line:
            return sdef.para.line
    return DEFAULT_LINE


def add_after(paragraph: etree._Element, points: float) -> None:
    """Add spacing-after to a paragraph built by ``ox.para`` (used before a table)."""
    w_ns = (
        paragraph.nsmap.get("w") or "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    )
    ppr = paragraph.find(f"{{{w_ns}}}pPr")
    spacing = ppr.find(f"{{{w_ns}}}spacing")
    if spacing is None:
        spacing = el("w:spacing")
        anchor = ppr.find(f"{{{w_ns}}}ind")
        if anchor is None:
            anchor = ppr.find(f"{{{w_ns}}}jc")
        if anchor is not None:
            anchor.addprevious(spacing)
        else:
            ppr.append(spacing)
    spacing.set(f"{{{w_ns}}}after", tw(points))
