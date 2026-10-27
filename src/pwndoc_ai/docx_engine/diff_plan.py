"""Word-level diff between two paragraph texts, as non-overlapping character edits.

The diff has a hard cost bound. ``difflib.SequenceMatcher`` is cubic on repetitive text, so before
it runs the worst-case cost is estimated; above the per-paragraph allowance the paragraphs are
diffed sentence by sentence first, and a stretch that is still too costly (or a build that has
spent its budget) becomes ONE coarse edit from its first to its last differing token. A coarse
edit is always correct (it turns ``old`` into ``new``), it only marks more text as changed.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import accumulate

from . import limits

TOKEN = re.compile(r"\s+|\w+|[^\w\s]", re.UNICODE)
SENTENCE_END = frozenset(".!?")
Opcode = tuple[int, int, int, int]  # old[i1:i2] becomes new[j1:j2], in token indexes


@dataclass(frozen=True)
class Edit:
    """Replace ``old[start:end]`` by ``insert`` (a pure insertion has ``start == end``, a pure
    deletion an empty ``insert``)."""

    start: int
    end: int
    insert: str


class DiffBudget:
    """Diff steps one build may still spend (shared by all of its paragraphs)."""

    def __init__(self, total: int | None = None) -> None:
        self.left = limits.DIFF_STEPS_PER_BUILD if total is None else total

    def allowance(self) -> int:
        return min(limits.DIFF_STEPS_PER_PARAGRAPH, self.left)

    def spend(self, steps: int) -> None:
        self.left = max(0, self.left - steps)


class _Steps:
    """The allowance of ONE paragraph; ``take`` refuses work that would exceed it."""

    def __init__(self, allowed: int) -> None:
        self.left = allowed
        self.used = 0

    def take(self, cost: int) -> bool:
        if cost > self.left:
            return False
        self.left -= cost
        self.used += cost
        return True


def tokenize(text: str) -> list[str]:
    return TOKEN.findall(text)


def _offsets(tokens: list[str]) -> list[int]:
    return [0, *accumulate(len(token) for token in tokens)]


def _trim(a: Sequence[str], b: Sequence[str]) -> tuple[int, int, int]:
    """(common prefix length, end of the differing part in a, end of it in b)."""
    start = 0
    while start < len(a) and start < len(b) and a[start] == b[start]:
        start += 1
    end_a, end_b = len(a), len(b)
    while end_a > start and end_b > start and a[end_a - 1] == b[end_b - 1]:
        end_a -= 1
        end_b -= 1
    return start, end_a, end_b


def _cost(a: Sequence[str], b: Sequence[str]) -> int:
    """Upper bound of SequenceMatcher's work: every one of its (at most min(n, m)) calls scans
    the matching pairs once."""
    counts = Counter(b)
    pairs = sum(counts[item] for item in a if item in counts)
    return pairs * min(len(a), len(b)) + len(a) + len(b)


def _coarse(a: Sequence[str], b: Sequence[str]) -> list[Opcode]:
    start, end_a, end_b = _trim(a, b)
    if start == end_a == end_b:
        return []
    return [(start, end_a, start, end_b)]


def _matcher_ops(a: Sequence[str], b: Sequence[str], steps: _Steps) -> list[Opcode] | None:
    """Exact non-equal opcodes of ``a`` against ``b``; None when that would cost too much."""
    start, end_a, end_b = _trim(a, b)
    mid_a, mid_b = a[start:end_a], b[start:end_b]
    if not mid_a or not mid_b:
        return _coarse(a, b)
    if not steps.take(_cost(mid_a, mid_b)):
        return None
    matcher = difflib.SequenceMatcher(None, mid_a, mid_b, autojunk=False)
    return [
        (start + i1, start + i2, start + j1, start + j2)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]


def _sentences(tokens: list[str]) -> tuple[list[str], list[int]]:
    """Sentence texts and the token index where each starts (a sentence ends after the
    whitespace that follows ``.``, ``!`` or ``?``, or after a line break)."""
    texts: list[str] = []
    starts: list[int] = []
    first = 0
    for index, token in enumerate(tokens):
        if token.isspace() and ("\n" in token or (index > 0 and tokens[index - 1] in SENTENCE_END)):
            texts.append("".join(tokens[first : index + 1]))
            starts.append(first)
            first = index + 1
    if first < len(tokens):
        texts.append("".join(tokens[first:]))
        starts.append(first)
    starts.append(len(tokens))
    return texts, starts


def _sentence_ops(a: list[str], b: list[str], steps: _Steps) -> list[Opcode]:
    texts_a, starts_a = _sentences(a)
    texts_b, starts_b = _sentences(b)
    blocks = _matcher_ops(texts_a, texts_b, steps)
    if blocks is None:
        return _coarse(a, b)
    ops: list[Opcode] = []
    for s1, s2, t1, t2 in blocks:
        i1, i2, j1, j2 = starts_a[s1], starts_a[s2], starts_b[t1], starts_b[t2]
        inner = _matcher_ops(a[i1:i2], b[j1:j2], steps)
        if inner is None:
            inner = _coarse(a[i1:i2], b[j1:j2])
        ops.extend((i1 + x1, i1 + x2, j1 + y1, j1 + y2) for x1, x2, y1, y2 in inner)
    return ops


def _opcodes(a: list[str], b: list[str], steps: _Steps) -> list[Opcode]:
    exact = _matcher_ops(a, b, steps)
    return exact if exact is not None else _sentence_ops(a, b, steps)


def _edits(old: str, new: str, a: list[str], b: list[str], ops: list[Opcode]) -> list[Edit]:
    off_a, off_b = _offsets(a), _offsets(b)
    return [Edit(off_a[i1], off_a[i2], new[off_b[j1] : off_b[j2]]) for i1, i2, j1, j2 in ops]


def _merge(old: str, edits: list[Edit]) -> list[Edit]:
    """Join edits that are separated by nothing or by whitespace only (one change, not two)."""
    merged: list[Edit] = []
    for edit in edits:
        if merged:
            previous = merged[-1]
            gap = old[previous.end : edit.start]
            if not gap.strip():
                merged[-1] = Edit(previous.start, edit.end, previous.insert + gap + edit.insert)
                continue
        merged.append(edit)
    return merged


def plan_edits(old: str, new: str, budget: DiffBudget | None = None) -> list[Edit]:
    """Ordered edits that turn ``old`` into ``new`` (tokens: words, spaces, punctuation).

    ``budget`` is shared by the paragraphs of one build; without one the paragraph gets its own.
    """
    budget = budget if budget is not None else DiffBudget()
    a, b = tokenize(old), tokenize(new)
    steps = _Steps(budget.allowance())
    ops = _opcodes(a, b, steps)
    budget.spend(steps.used)
    return _merge(old, _edits(old, new, a, b, ops))
