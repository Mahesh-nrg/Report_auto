"""Lines to document blocks: title, headings (size/bold clustering relative to the body size),
paragraphs (joined, de-hyphenated), list items, code, captions, TOC lines, tables and images."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace

from .extract import Box
from .lines import Line
from .tables import Table

SYMBOL_BULLETS = "\uf0b7\uf0a7\uf076\uf0d8\uf0fc\uf06e"  # Symbol/Wingdings private-use bullets
BULLET = re.compile(rf"^\s*([•●▪■◦‣⁃·–—\-\*o{SYMBOL_BULLETS}])\s*(?<=\s|[{SYMBOL_BULLETS}])(?=\S)")
NUMBERED = re.compile(r"^\s*(\(?\d{1,3}[.)]|\(?[a-zA-Z][.)]|\(?[ivxIVX]{1,5}[.)])\s+(?=\S)")
HEADNUM = re.compile(r"^\s*(\d{1,2}(\.\d{1,2}){0,4}|chapter\s+\d+|appendix\s+[a-z])[.\s:)]", re.I)
LEADER = re.compile(r"(\.{4,}|…{2,}|(\.\s){4,})\s*[\divxlc]+\s*$", re.I)
CAPTION = re.compile(r"^(figure|fig\.|table|listing|exhibit)\s+[\dA-Z][\d.\-]*[.:\s]", re.I)
TERMINAL = tuple('.!?:;”")')
MAX_LEVELS = 4
HEADING_SHARE_LIMIT = 0.25  # more candidates than this share of lines: a form or a poster
Key = tuple[float, bool]


@dataclass(frozen=True)
class Block:
    kind: str  # title heading para list code caption toc table image
    page: int
    text: str = ""
    level: int = 0
    ordered: bool = False
    table: Table | None = None
    bbox: Box | None = None


@dataclass(frozen=True)
class Item:
    """One element of a page in reading order: a line, a table or an image box."""

    y: float
    page: int
    line: Line | None = None
    table: Table | None = None
    image: Box | None = None


def half(size: float) -> float:
    return round(size * 2) / 2


def body_size(lines: Sequence[Line]) -> float:
    """The size with the most characters (mono text excluded); 11 pt when there is none."""
    counts: Counter = Counter()
    for line in lines:
        if not line.mono:
            counts[half(line.size)] += len(line.text)
    return counts.most_common(1)[0][0] if counts else 11.0


def heading_key(line: Line, body: float) -> Key | None:
    text = line.text.rstrip()
    if line.mono or len(text) > 140 or LEADER.search(text) or text.endswith((",", ";")):
        return None
    if BULLET.match(text) and not line.bold:
        return None
    big = line.size >= body * 1.12
    bold = line.bold and line.size >= body * 0.95 and len(text) <= 90 and not text.endswith(".")
    return (half(line.size), line.bold) if big or bold else None


@dataclass(frozen=True)
class HeadingPlan:
    keys: dict[int, Key]  # id(line) -> key
    title: frozenset[Key]
    levels: dict[Key, int]


def plan_headings(lines: Sequence[Line], body: float, pages: int) -> HeadingPlan:
    keys = {id(ln): k for ln in lines if (k := heading_key(ln, body)) is not None}
    where: dict[Key, list[int]] = {}
    for line in lines:
        if id(line) in keys:
            where.setdefault(keys[id(line)], []).append(line.page)
    others = [k[0] for k, p in where.items() if set(p) != {1}]
    title = frozenset(
        k
        for k, p in where.items()
        if set(p) == {1} and len(p) <= 4 and k[0] >= body * 1.5 and pages > 2
        and k[0] > max(others, default=0)
    )  # fmt: skip
    if len(keys) > HEADING_SHARE_LIMIT * max(1, len(lines)):
        return HeadingPlan({}, frozenset(), {})
    ranked = sorted({k for k in keys.values() if k not in title}, key=lambda k: (-k[0], not k[1]))
    levels = {k: min(i + 1, MAX_LEVELS) for i, k in enumerate(ranked)}
    return HeadingPlan(keys, title, levels)


def join_text(left: str, right: str) -> str:
    if left.endswith("-") and right[:1].islower() and len(left) > 2 and left[-2].isalpha():
        return left[:-1] + right
    return f"{left} {right}"


class Assembler:
    """Walks items in reading order and builds blocks (internal, short-lived state)."""

    def __init__(self, plan: HeadingPlan, body: float) -> None:
        self.plan, self.body = plan, body
        self.blocks: list[Block] = []
        self.current: Block | None = None
        self.prev: Line | None = None
        self.width = 0.0

    def close(self) -> None:
        if self.current is not None:
            self.blocks.append(self.current)
        self.current, self.prev = None, None

    def start(self, block: Block, line: Line | None) -> None:
        self.close()
        self.current, self.prev = block, line
        self.width = line.width if line else 0.0

    def extend(self, text: str, line: Line, sep: str = " ") -> None:
        assert self.current is not None
        joined = (
            join_text(self.current.text, text) if sep == " " else self.current.text + sep + text
        )
        self.current = replace(self.current, text=joined)
        self.prev, self.width = line, max(self.width, line.width)

    def add(self, item: Item) -> None:
        if item.table is not None:
            self.close()
            self.blocks.append(Block("table", item.page, table=item.table))
        elif item.image is not None:
            self.close()
            self.blocks.append(Block("image", item.page, bbox=item.image))
        elif item.line is not None:
            self.line(item.line)

    def line(self, line: Line) -> None:
        key = self.plan.keys.get(id(line))
        if key is not None and key in self.plan.title:
            self.heading(line, "title", 0)
        elif key is not None and key in self.plan.levels:
            self.heading(line, "heading", self.plan.levels[key])
        elif line.mono:
            self.code(line)
        elif LEADER.search(line.text):
            self.close()
            self.blocks.append(Block("toc", line.page, line.text.strip()))
        elif CAPTION.match(line.text) and (line.italic or line.bold or line.size < self.body):
            self.start(Block("caption", line.page, line.text.strip()), line)
        elif not self.list_item(line) and not self.continues(line):
            self.start(Block("para", line.page, line.text.strip()), line)

    def heading(self, line: Line, kind: str, level: int) -> None:
        cur, prev = self.current, self.prev
        same = cur is not None and cur.kind == kind and cur.level == level and prev is not None
        wrapped = (
            same and abs(line.base - prev.base) < (1.8 if kind == "title" else 1.5) * line.size
        )
        if wrapped and (kind == "title" or not HEADNUM.match(line.text)):
            self.extend(line.text.strip(), line)
            return
        self.start(Block(kind, line.page, line.text.strip(), level), line)

    def code(self, line: Line) -> None:
        cur, prev = self.current, self.prev
        if cur is not None and cur.kind == "code" and prev is not None:
            if line.y0 - prev.y1 < 1.5 * line.size:
                self.extend(line.text, line, "\n")
                return
        self.start(Block("code", line.page, line.text), line)

    def list_item(self, line: Line) -> bool:
        match = BULLET.match(line.text) or NUMBERED.match(line.text)
        if not match or len(line.text) <= match.end():
            return False
        ordered = bool(NUMBERED.match(line.text)) and not BULLET.match(line.text)
        self.start(Block("list", line.page, line.text[match.end() :].strip(), 0, ordered), line)
        return True

    def continues(self, line: Line) -> bool:
        cur, prev = self.current, self.prev
        if cur is None or prev is None or cur.kind not in ("para", "list", "caption"):
            return False
        gap, same_page = line.base - prev.base, prev.page == line.page
        same_size = abs(line.size - prev.size) < 1.0
        short_prev = (
            same_page
            and prev.text.rstrip().endswith(TERMINAL)
            and prev.width < 0.7 * max(1.0, self.width)
        )
        if same_page and 0 < gap <= 1.65 * line.size and same_size and not short_prev:
            self.extend(line.text.strip(), line)
            return True
        jump = not same_page or gap < 0
        open_end = not prev.text.rstrip().endswith(TERMINAL) and line.text[:1].islower()
        if jump and open_end and same_size:
            self.extend(line.text.strip(), line)
            return True
        return False


def assemble(pages: Sequence[Sequence[Item]], plan: HeadingPlan, body: float) -> list[Block]:
    builder = Assembler(plan, body)
    for items in pages:
        for item in items:
            builder.add(item)
    builder.close()
    return builder.blocks


def page_items(
    lines: Sequence[Line], tables: Sequence[Table], images: Sequence[Box], page: int
) -> list[Item]:
    """Lines keep their (column) order; tables and images are slotted in before the first line
    below their top edge."""
    pending = sorted(
        [Item(t.bbox[1], page, table=t) for t in tables]
        + [Item(b[1], page, image=b) for b in images],
        key=lambda item: item.y,
    )
    out: list[Item] = []
    for line in lines:
        while pending and pending[0].y <= line.y0 - 1:
            out.append(pending.pop(0))
        out.append(Item(line.y0, page, line=line))
    return out + pending


__all__ = [
    "Block",
    "HeadingPlan",
    "Item",
    "assemble",
    "body_size",
    "heading_key",
    "join_text",
    "page_items",
    "plan_headings",
]
