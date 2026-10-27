"""Table detection: grid tables from ruling lines (with merged cells) and borderless tables from
aligned rows. Work is bounded by ``MAX_SEGMENTS_TABLES`` (union-find is quadratic)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from . import limits
from .extract import Box, Page, Rule
from .lines import Line

TOL = 2.5
LEADER = re.compile(r"(\.{4,}|…{2,}|(\.\s){4,})\s*[\divxlc]+\s*$", re.I)
Span2 = tuple[int, int]  # (rowspan, colspan); (0, 0) marks a covered cell


@dataclass(frozen=True)
class Table:
    bbox: Box
    rows: int
    cols: int
    cells: tuple[tuple[str, ...], ...]
    spans: tuple[tuple[Span2, ...], ...]


def _merge(values: Sequence[float], tol: float = 2.0) -> list[float]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if groups and value - groups[-1][-1] <= tol:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(g) / len(g) for g in groups]


def _touch(a: tuple, b: tuple) -> bool:
    if a[0] == b[0]:
        return abs(a[1] - b[1]) <= TOL and a[2] - TOL <= b[3] and b[2] - TOL <= a[3]
    h, v = (a, b) if a[0] == "h" else (b, a)
    return h[2] - TOL <= v[1] <= h[3] + TOL and v[2] - TOL <= h[1] <= v[3] + TOL


def _groups(hrules: Sequence[Rule], vrules: Sequence[Rule]) -> list[list[tuple]]:
    segs = [("h", *s) for s in hrules if s[2] - s[1] > 6]
    segs += [("v", *s) for s in vrules if s[2] - s[1] > 6]
    segs = segs[: limits.MAX_SEGMENTS_TABLES]
    parent = list(range(len(segs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(segs):
        for j in range(i + 1, len(segs)):
            if _touch(a, segs[j]):
                parent[find(i)] = find(j)
    groups: dict[int, list[tuple]] = {}
    for i, seg in enumerate(segs):
        groups.setdefault(find(i), []).append(seg)
    return list(groups.values())


def _covered(segs: Sequence[tuple], pos: float, low: float, high: float) -> bool:
    """True when the segments at ``pos`` cover ``[low, high]``."""
    current = low
    for start, end in sorted((s[2], s[3]) for s in segs if abs(s[1] - pos) <= TOL):
        if start <= current + TOL:
            current = max(current, end)
    return current >= high - TOL


@dataclass(frozen=True)
class _Grid:
    bbox: Box
    ys: tuple[float, ...]
    xs: tuple[float, ...]
    hsegs: tuple[tuple, ...]
    vsegs: tuple[tuple, ...]


def _grid(group: list[tuple], page: Page) -> _Grid | None:
    hs = [s for s in group if s[0] == "h"]
    vs = [s for s in group if s[0] == "v"]
    ys, xs = _merge([s[1] for s in hs]), _merge([s[1] for s in vs])
    if len(ys) < 2 or len(xs) < 2:
        return None
    bbox = (
        min(xs[0], min(s[2] for s in hs)), min(ys[0], min(s[2] for s in vs)),
        max(xs[-1], max(s[3] for s in hs)), max(ys[-1], max(s[3] for s in vs)),
    )  # fmt: skip
    if bbox[2] - bbox[0] > 0.97 * page.width and bbox[3] - bbox[1] > 0.95 * page.height:
        return None  # the page frame
    ys = [y for y in ys if bbox[1] - 2 <= y <= bbox[3] + 2]
    xs = [x for x in xs if bbox[0] - 2 <= x <= bbox[2] + 2]
    if len(ys) < 2 or len(xs) < 2 or (len(ys) - 1) * (len(xs) - 1) > 2500:
        return None
    return _Grid(bbox, tuple(ys), tuple(xs), tuple(hs), tuple(vs))


def _place(grid: _Grid, line: Line) -> tuple[int, int] | None:
    cx, cy = (line.x0 + line.x1) / 2, (line.y0 + line.y1) / 2
    box = grid.bbox
    if not (box[0] - 2 <= cx <= box[2] + 2 and box[1] - 2 <= cy <= box[3] + 2):
        return None
    ys, xs = grid.ys, grid.xs
    row = next((i for i in range(len(ys) - 1) if ys[i] - 2 <= cy <= ys[i + 1] + 2), None)
    left = line.x0 + 2
    col = next((j for j in range(len(xs) - 1) if xs[j] - 2 <= left <= xs[j + 1] + 2), None)
    return None if row is None or col is None else (row, col)


def _spans(grid: _Grid) -> tuple[tuple[Span2, ...], ...]:
    rows, cols = len(grid.ys) - 1, len(grid.xs) - 1
    table = [[(1, 1)] * cols for _ in range(rows)]
    covered: set[tuple[int, int]] = set()
    for r in range(rows):
        for c in range(cols):
            if (r, c) in covered:
                table[r][c] = (0, 0)
                continue
            cs = 1
            while c + cs < cols and not _covered(grid.vsegs, grid.xs[c + cs], *grid.ys[r : r + 2]):
                cs += 1
            rs = 1
            while r + rs < rows and not _covered(grid.hsegs, grid.ys[r + rs], *grid.xs[c : c + 2]):
                rs += 1
            table[r][c] = (rs, cs)
            covered |= {(rr, cc) for rr in range(r, r + rs) for cc in range(c, c + cs)} - {(r, c)}
    return tuple(tuple(row) for row in table)


def _join(lines: Sequence[Line]) -> str:
    ordered = sorted(lines, key=lambda ln: (round(ln.base / 2), ln.x0))
    return " ".join(ln.text.strip() for ln in ordered)


def _fill(grid: _Grid, lines: Sequence[Line]) -> tuple[Table | None, list[Line]]:
    rows, cols = len(grid.ys) - 1, len(grid.xs) - 1
    cells: list[list[list[Line]]] = [[[] for _ in range(cols)] for _ in range(rows)]
    rest: list[Line] = []
    for line in lines:
        spot = _place(grid, line)
        if spot is None:
            rest.append(line)
        else:
            cells[spot[0]][spot[1]].append(line)
    if len(rest) == len(lines):
        return None, rest
    text = tuple(tuple(_join(cell) for cell in row) for row in cells)
    return Table(grid.bbox, rows, cols, text, _spans(grid)), rest


def grid_tables(page: Page, lines: Sequence[Line]) -> tuple[list[Table], list[Line]]:
    """Ruled tables of a page and the lines left outside them."""
    tables: list[Table] = []
    rest = list(lines)
    for group in _groups(page.hrules, page.vrules):
        grid = _grid(group, page)
        if grid is None:
            continue
        table, rest = _fill(grid, rest)
        if table is not None:
            tables.append(table)
    return tables, rest


def baseline_rows(lines: Sequence[Line]) -> list[list[Line]]:
    rows: list[list[Line]] = []
    for line in sorted(lines, key=lambda ln: (round(ln.base), ln.x0)):
        if rows and abs(rows[-1][0].base - line.base) <= 0.4 * max(line.size, 1.0):
            rows[-1].append(line)
        else:
            rows.append([line])
    return rows


def _aligned(run: list[list[Line]]) -> Table | None:
    cols = _merge([c.x0 for row in run for c in row], tol=6.0)
    cells_n = sum(len(row) for row in run)
    hits = sum(1 for row in run for c in row if min(abs(c.x0 - x) for x in cols) <= 6)
    if not (
        2 <= len(cols) <= 12 and hits >= 0.9 * cells_n and cells_n >= 0.6 * len(run) * len(cols)
    ):
        return None
    grid: list[list[list[Line]]] = [[[] for _ in cols] for _ in run]
    for i, row in enumerate(run):
        for cell in row:
            grid[i][min(range(len(cols)), key=lambda j: abs(cols[j] - cell.x0))].append(cell)
    every = [c for row in run for c in row]
    bbox = (
        min(c.x0 for c in every), min(c.y0 for c in run[0]),
        max(c.x1 for c in every), max(c.y1 for c in run[-1]),
    )  # fmt: skip
    text = tuple(tuple(_join(cell) for cell in row) for row in grid)
    spans = tuple(tuple((1, 1) for _ in cols) for _ in run)
    return Table(bbox, len(run), len(cols), text, spans)


def _multi(row: list[Line]) -> bool:
    return len(row) >= 2 and all(len(c.text) < 80 and not LEADER.search(c.text) for c in row)


def borderless_tables(lines: Sequence[Line], min_rows: int = 3) -> tuple[list[Table], list[Line]]:
    """Runs of at least ``min_rows`` multi-cell rows whose cells line up (6 pt)."""
    tables: list[Table] = []
    taken: set[int] = set()
    run: list[list[Line]] = []

    def flush() -> None:
        table = _aligned(run) if len(run) >= min_rows else None
        if table is not None:
            tables.append(table)
            taken.update(id(c) for row in run for c in row)
        run.clear()

    for row in baseline_rows(lines):
        if not _multi(row):
            flush()
            continue
        if run and not (0 < row[0].base - run[-1][0].base <= 2.4 * max(row[0].size, 1.0)):
            flush()
        run.append(row)
    flush()
    return tables, [ln for ln in lines if id(ln) not in taken]


__all__ = ["Table", "baseline_rows", "borderless_tables", "grid_tables"]
