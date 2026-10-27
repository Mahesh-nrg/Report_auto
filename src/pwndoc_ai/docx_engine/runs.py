"""Paragraph text and run surgery: read text as characters, split runs at character offsets."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator
from dataclasses import dataclass

from lxml import etree

from .xmlutil import SPACE, w

TAB, NEWLINE = "\t", "\n"
_SPECIAL = re.compile(r"([\t\n])")
P, R, T, RPR = w("p"), w("r"), w("t"), w("rPr")
HYPERLINK = w("hyperlink")


@dataclass(frozen=True)
class Span:
    """A run that carries text, with its character range inside the paragraph text."""

    run: etree._Element
    start: int
    end: int


def iter_runs(paragraph: etree._Element) -> Iterator[etree._Element]:
    """Runs that are direct children of the paragraph or of one of its hyperlinks."""
    for child in paragraph:
        if child.tag == R:
            yield child
        elif child.tag == HYPERLINK:
            yield from (grand for grand in child if grand.tag == R)


def run_text(run: etree._Element) -> str:
    parts: list[str] = []
    for child in run:
        if child.tag == T:
            parts.append(child.text or "")
        elif child.tag == w("tab"):
            parts.append(TAB)
        elif child.tag == w("br"):
            parts.append(NEWLINE)
    return "".join(parts)


def text_spans(paragraph: etree._Element) -> list[Span]:
    spans: list[Span] = []
    position = 0
    for run in iter_runs(paragraph):
        text = run_text(run)
        if text:
            spans.append(Span(run, position, position + len(text)))
            position += len(text)
    return spans


def paragraph_text(paragraph: etree._Element) -> str:
    return "".join(run_text(run) for run in iter_runs(paragraph))


def _text_element(text: str, tag: str = "t") -> etree._Element:
    element = etree.Element(w(tag))
    element.text = text
    element.set(SPACE, "preserve")
    return element


def content_elements(text: str, tag: str = "t") -> list[etree._Element]:
    """Run content for ``text``: w:t pieces with w:tab and w:br for TAB and NEWLINE."""
    out: list[etree._Element] = []
    for piece in _SPECIAL.split(text):
        if piece == TAB:
            out.append(etree.Element(w("tab")))
        elif piece == NEWLINE:
            out.append(etree.Element(w("br")))
        elif piece:
            out.append(_text_element(piece, tag))
    return out


def set_run_text(run: etree._Element, text: str) -> None:
    """Replace the content of a run by ``text``; the run properties are kept."""
    for child in list(run):
        if child.tag != RPR:
            run.remove(child)
    run.extend(content_elements(text))


def split_run(run: etree._Element, offset: int) -> etree._Element:
    """Cut ``run`` at ``offset`` characters; ``run`` keeps the head and the new tail is returned
    (a sibling placed right after it, with a copy of the run properties)."""
    text = run_text(run)
    tail = etree.Element(R)
    properties = run.find(RPR)
    if properties is not None:
        tail.append(copy.deepcopy(properties))
    tail.extend(content_elements(text[offset:]))
    set_run_text(run, text[:offset])
    run.addnext(tail)
    return tail


def new_run(properties: etree._Element | None, text: str) -> etree._Element:
    run = etree.Element(R)
    if properties is not None:
        run.append(copy.deepcopy(properties))
    run.extend(content_elements(text))
    return run


def to_deleted_text(run: etree._Element) -> None:
    """Inside a w:del the text elements must be w:delText."""
    for element in run.iter(T):
        element.tag = w("delText")
        element.set(SPACE, "preserve")
