"""Spans to lines, running header/footer removal and reading order for one or two columns."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from .extract import Page, Span

DIGITS = re.compile(r"\d+")
NUMBER_TOKEN = re.compile(r"\s*\(?\w{1,3}[.)]")
PAGE_ONLY = frozenset({"#", "- # -", "page #", "# / #", "page # of #", "#/#"})
BAND = 0.09  # top/bottom share of the page where running lines live
RUNNING_SHARE = 0.4
RUNNING_MIN = 3


@dataclass(frozen=True)
class Line:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    base: float
    size: float
    bold: bool
    italic: bool
    mono: bool
    text: str
    fonts: tuple[str, ...] = ()

    @property
    def width(self) -> float:
        return self.x1 - self.x0


def _joins(last: list[Span], span: Span) -> bool:
    prev = last[-1]
    size = max(prev.size, span.size, 1.0)
    return (
        span.block == prev.block
        and abs(prev.base - span.base) <= 0.35 * size
        and span.x0 >= prev.x0 - 1
        and span.x0 - prev.x1 < 1.6 * max(span.size, 1.0)
    )


def _text_of(group: list[Span]) -> str:
    text = group[0].text
    for prev, span in zip(group, group[1:], strict=False):
        gap = span.x0 - prev.x1
        numbered = NUMBER_TOKEN.fullmatch(text) is not None
        wide = gap > 0.15 * span.size or (numbered and gap > 0.02)
        edge = text.endswith(" ") or span.text.startswith(" ")
        text += (" " if wide and not edge else "") + span.text
    return text.strip()


def _line_of(page: int, group: list[Span]) -> Line:
    inked = [s for s in group if s.text.strip()] or group
    long_spans = [s.size for s in inked if len(s.text.strip()) > 2] or [s.size for s in inked]
    return Line(
        page=page,
        x0=min(s.x0 for s in group), y0=min(s.y0 for s in group),
        x1=max(s.x1 for s in group), y1=max(s.y1 for s in group),
        base=group[0].base, size=max(long_spans),
        bold=all(s.bold for s in inked), italic=all(s.italic for s in inked),
        mono=all(s.mono for s in inked), text=_text_of(group),
        fonts=tuple(dict.fromkeys(s.font for s in inked)),
    )  # fmt: skip


def make_lines(page: Page) -> list[Line]:
    """Lines of one page in top-to-bottom, left-to-right order."""
    spans = sorted(page.spans, key=lambda s: (s.block, round(s.base), s.x0))
    groups: list[list[Span]] = []
    for span in spans:
        if groups and _joins(groups[-1], span):
            groups[-1].append(span)
        else:
            groups.append([span])
    lines = [_line_of(page.number, group) for group in groups]
    lines = [line for line in lines if line.text]
    return sorted(lines, key=lambda line: (round(line.base / 2), line.x0))


def running_key(line: Line, page: Page) -> tuple[str, str] | None:
    """(band, digit-normalised text) of a line in the top or bottom band, else None."""
    if line.y1 < BAND * page.height:
        band = "T"
    elif line.y0 > (1 - BAND) * page.height:
        band = "B"
    else:
        return None
    return band, DIGITS.sub("#", " ".join(line.text.split())).casefold()


def running_keys(pages: Sequence[Page], lines: Sequence[Sequence[Line]]) -> frozenset:
    """Band lines repeated on at least 40% of the pages (minimum 3), and page numbers."""
    if len(pages) < RUNNING_MIN:
        return frozenset()
    counts: Counter = Counter()
    for page, page_lines in zip(pages, lines, strict=False):
        counts.update({k for k in (running_key(ln, page) for ln in page_lines) if k})
    need = max(RUNNING_MIN, int(RUNNING_SHARE * len(pages)))
    keys = {key for key, seen in counts.items() if seen >= need}
    keys |= {key for key, seen in counts.items() if key[1] in PAGE_ONLY and seen >= 2}
    return frozenset(keys)


def _gutter(lines: Sequence[Line], page: Page) -> float | None:
    narrow = [line for line in lines if line.width < 0.55 * page.width]
    if len(narrow) < 8:
        return None
    best: float | None = None
    middle = page.width / 2
    for gx in range(int(page.width * 0.35), int(page.width * 0.65), 3):
        cross = sum(1 for ln in narrow if ln.x0 < gx - 4 < gx + 4 < ln.x1)
        left = sum(1 for ln in narrow if ln.x1 <= gx + 4)
        right = sum(1 for ln in narrow if ln.x0 >= gx - 4)
        if cross <= 0.12 * len(narrow) and min(left, right) >= 0.3 * len(narrow):
            if best is None or abs(gx - middle) < abs(best - middle):
                best = float(gx)
    return best


def order_columns(lines: Sequence[Line], page: Page) -> tuple[list[Line], int]:
    """Reading order and the column count (1 or 2). Full-width lines are barriers: each band
    between them is emitted left column first."""
    gutter = _gutter(lines, page)
    if gutter is None:
        return list(lines), 1
    out: list[Line] = []
    band: list[Line] = []

    def flush() -> None:
        left = sorted((ln for ln in band if ln.x1 <= gutter + 4), key=lambda ln: ln.y0)
        right = sorted((ln for ln in band if ln.x1 > gutter + 4), key=lambda ln: ln.y0)
        out.extend(left + right)
        band.clear()

    for line in sorted(lines, key=lambda ln: ln.y0):
        if line.x0 < gutter - 4 < gutter + 4 < line.x1:
            flush()
            out.append(line)
        else:
            band.append(line)
    flush()
    return out, 2


def column_estimate(lines: Sequence[Line], page: Page) -> int:
    """Columns of narrow text: distinct left edges of lines under a third of the page width
    that each hold at least 15% of those lines (1 when the page is mostly full-width)."""
    narrow = [ln for ln in lines if ln.width < page.width / 3 and len(ln.text) > 20]
    if len(narrow) < 12 or len(narrow) < 0.5 * len(lines):
        return 1
    edges = Counter(round(ln.x0 / 12) for ln in narrow)
    return max(1, sum(1 for seen in edges.values() if seen >= 0.15 * len(narrow)))


__all__ = [
    "Line",
    "column_estimate",
    "make_lines",
    "order_columns",
    "running_key",
    "running_keys",
]
