"""Apply a new text to a paragraph in place, keeping the original runs and their formatting.

Unchanged spans keep their original runs (split at character offsets only where a change starts
or ends). In tracked mode a deleted span is wrapped in ``w:del`` (``w:delText``) and an inserted
span becomes a ``w:ins`` run formatted like the neighbouring original text. In clean mode the
same edit is applied with every change accepted. These functions mutate the element they are
given; the engine always works on its own parsed copy of the document.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass

from lxml import etree

from .diff_plan import Edit, plan_edits
from .errors import DocxError
from .protect import structural_reason
from .revisions import IdSource, revision
from .runs import (
    HYPERLINK,
    RPR,
    Span,
    new_run,
    run_text,
    split_run,
    text_spans,
    to_deleted_text,
)
from .xmlutil import w

__all__ = ["IdSource", "add_paragraph_after", "apply_paragraph_edit"]

UNSAFE_PARAGRAPH = "This paragraph cannot be edited safely."
HYPERLINK_STYLE = "Hyperlink"


@dataclass(frozen=True)
class Context:
    track: bool
    author: str
    timestamp: str
    id_source: IdSource


@dataclass
class Piece:
    """A run (or part of one) and its character range in the paragraph text."""

    run: etree._Element
    start: int
    end: int


def _split_pieces(spans: list[Span], edits: list[Edit]) -> list[Piece]:
    """Cut runs so that every edit boundary is a run boundary."""
    cuts = sorted({offset for edit in edits for offset in (edit.start, edit.end)})
    pieces: list[Piece] = []
    for span in spans:
        run, start = span.run, span.start
        for cut in cuts:
            if start < cut < span.end:
                tail = split_run(run, cut - start)
                pieces.append(Piece(run, start, cut))
                run, start = tail, cut
        pieces.append(Piece(run, start, span.end))
    return pieces


def _strip_link_style(run: etree._Element) -> None:
    style = run.find(f"{w('rPr')}/{w('rStyle')}")
    if style is not None and style.get(w("val")) == HYPERLINK_STYLE:
        style.getparent().remove(style)


def _inserted(ctx: Context, props: etree._Element | None, text: str) -> etree._Element:
    run = new_run(props, text)
    if not ctx.track:
        return run
    mark = revision("ins", ctx.id_source, ctx.author, ctx.timestamp)
    mark.append(run)
    return mark


def _remove_run(run: etree._Element) -> None:
    parent = run.getparent()
    parent.remove(run)
    if parent.tag == HYPERLINK and not any(c.tag == w("r") for c in parent):
        parent.getparent().remove(parent)


def _wrap_deleted(run: etree._Element, ctx: Context, created: set[etree._Element]) -> None:
    to_deleted_text(run)
    previous = run.getprevious()
    if previous is not None and previous in created:
        previous.append(run)
        return
    mark = revision("del", ctx.id_source, ctx.author, ctx.timestamp)
    run.addprevious(mark)
    mark.append(run)
    created.add(mark)


def _props(run: etree._Element) -> etree._Element | None:
    return run.find(RPR)


def _place_between(node: etree._Element, before: Piece | None, after: Piece | None) -> None:
    """Put an insertion between two original pieces: after the earlier one, unless that would
    extend a hyperlink the later piece is not part of (then right after the link)."""
    if before is not None:
        anchor = before.run
        parent = anchor.getparent()
        same_link = after is not None and after.run.getparent() is parent
        if parent.tag == HYPERLINK and not same_link:
            anchor = parent
        anchor.addnext(node)
    elif after is not None:
        parent = after.run.getparent()
        (parent if parent.tag == HYPERLINK else after.run).addprevious(node)


def _locate(edit: Edit, pieces: list[Piece]) -> tuple[list[Piece], Piece | None, Piece | None]:
    """The pieces an edit deletes, and the original text pieces right before and after it."""
    text = [p for p in pieces if p.start < p.end]
    deleted = [p for p in text if p.start >= edit.start and p.end <= edit.end]
    before = next((p for p in reversed(text) if p.end == edit.start), None)
    after = next((p for p in text if p.start == edit.end), None)
    return deleted, before, after


def _insert(
    edit: Edit,
    located: tuple[list[Piece], Piece | None, Piece | None],
    paragraph: etree._Element,
    ctx: Context,
) -> None:
    deleted, before, after = located
    source = deleted[0] if deleted else (before or after)
    node = _inserted(ctx, _props(source.run) if source else None, edit.insert)
    if deleted:
        deleted[-1].run.addnext(node)  # a replacement goes where the old text was
    elif source is None:
        paragraph.append(node)
    else:
        _place_between(node, before, after)
        if node.getparent() is paragraph:  # outside any hyperlink: no link look
            for run in node.iter(w("r")):
                _strip_link_style(run)


def _apply_edit(
    edit: Edit,
    pieces: list[Piece],
    paragraph: etree._Element,
    ctx: Context,
    created: set[etree._Element],
) -> None:
    located = _locate(edit, pieces)
    if edit.insert:
        _insert(edit, located, paragraph, ctx)
    for piece in located[0]:
        if ctx.track:
            _wrap_deleted(piece.run, ctx, created)
        else:
            _remove_run(piece.run)


def apply_paragraph_edit(
    paragraph: etree._Element,
    new_text: str,
    *,
    track: bool,
    author: str,
    timestamp: str,
    id_source: IdSource,
    planner: Callable[[str, str], list[Edit]] = plan_edits,
) -> bool:
    """Turn the paragraph's text into ``new_text``. Returns False (and touches nothing) when
    the text is already equal. Raises DocxError for a paragraph that cannot be edited safely.
    ``planner(old, new)`` gives the edits (the build passes one that plans each text once)."""
    if structural_reason(paragraph):
        raise DocxError(UNSAFE_PARAGRAPH)
    spans = text_spans(paragraph)
    old_text = "".join(run_text(span.run) for span in spans)
    if new_text == old_text:
        return False
    edits = planner(old_text, new_text)
    ctx = Context(track, author, timestamp, id_source)
    pieces = _split_pieces(spans, edits)
    created: set[etree._Element] = set()
    for edit in edits:
        _apply_edit(edit, pieces, paragraph, ctx, created)
    return True


REVISION_TAGS = ("ins", "del", "moveFrom", "moveTo", "rPrChange")
MARKER_TAGS = frozenset(
    w(t)
    for t in ("bookmarkStart", "bookmarkEnd", "commentRangeStart", "commentRangeEnd", "proofErr")
)
UNSAFE_ADDITION = "A paragraph cannot be added after this one."


def _followed_by_paragraph(node: etree._Element) -> bool:
    """Whether the next block after ``node`` is a paragraph: only then can a paragraph mark that
    is rejected merge into something (not at the end of the body or a table cell, and not
    before a table)."""
    for sibling in node.itersiblings():
        if sibling.tag == w("p"):
            return True
        if sibling.tag not in MARKER_TAGS:
            return False
    return False


def can_add_after(paragraph: etree._Element, *, heading: bool) -> bool:
    """Whether an added paragraph rejects back to the original after ``paragraph``. Always true
    when a paragraph follows. Otherwise the new paragraph has to carry the anchor's own paragraph
    properties, which only works when they can be copied whole: not for a heading (the new text
    is body text), a section break, a forced page break, an outline level or a revision."""
    if _followed_by_paragraph(paragraph):
        return True
    if heading:
        return False
    props = paragraph.find(w("pPr"))
    if props is None:
        return True
    if any(
        props.find(w(tag)) is not None
        for tag in ("sectPr", "pageBreakBefore", "pPrChange", "outlineLvl")
    ):
        return False
    return not any(props.find(f"{w('rPr')}/{w(tag)}") is not None for tag in REVISION_TAGS)


def _clean_paragraph_props(
    anchor: etree._Element, ctx: Context, plain_body: bool
) -> etree._Element:
    """Properties of the added paragraph: a copy of the anchor's, minus anything that must not
    repeat (section break, page break, revision records), plus the inserted-mark when tracking."""
    source = anchor.find(w("pPr"))
    props = copy.deepcopy(source) if source is not None else etree.Element(w("pPr"))
    drop = ["sectPr", "pageBreakBefore", "pPrChange", "outlineLvl"]
    if plain_body:
        drop += ["pStyle", "numPr", "keepNext"]
    for tag in drop:
        for node in props.findall(w(tag)):
            props.remove(node)
    mark_props = props.find(w("rPr"))
    if mark_props is None:
        mark_props = etree.SubElement(props, w("rPr"))
    for node in list(mark_props):
        if node.tag in (w("ins"), w("del"), w("moveFrom"), w("moveTo"), w("rPrChange")):
            mark_props.remove(node)
    if ctx.track:
        mark_props.insert(0, revision("ins", ctx.id_source, ctx.author, ctx.timestamp))
    if len(mark_props) == 0:
        props.remove(mark_props)
    return props


def _mark_inserted(paragraph: etree._Element, ctx: Context) -> None:
    """Make ``paragraph``'s own paragraph mark an insertion (Word's form for text added at the
    end of a container: rejecting it joins the paragraph with the one that follows)."""
    props = paragraph.find(w("pPr"))
    if props is None:
        props = etree.Element(w("pPr"))
        paragraph.insert(0, props)
    mark_props = props.find(w("rPr"))
    if mark_props is None:
        mark_props = etree.SubElement(props, w("rPr"))
    mark_props.insert(0, revision("ins", ctx.id_source, ctx.author, ctx.timestamp))


def _carried_props(last: etree._Element, ctx: Context) -> etree._Element:
    """Properties for a paragraph added at the end of a container. It takes over ``last``'s
    original paragraph mark (a copy of its properties, no insertion), and ``last`` is the one
    whose mark becomes the insertion, so that rejecting the change gives back the original."""
    source = last.find(w("pPr"))
    carried = copy.deepcopy(source) if source is not None else etree.Element(w("pPr"))
    if ctx.track:
        _mark_inserted(last, ctx)
    return carried


def add_paragraph_after(
    anchor: etree._Element,
    text: str,
    *,
    track: bool,
    author: str,
    timestamp: str,
    id_source: IdSource,
    plain_body: bool = False,
    after: etree._Element | None = None,
) -> etree._Element:
    """Insert a new paragraph styled like ``anchor`` (``plain_body`` drops the style, for an
    anchor that is a heading), right after ``anchor`` or after ``after`` (an earlier addition).
    Tracked: the paragraph mark and its run are insertions, except at the end of a container
    (last paragraph of the body, of a table cell, before a table) where there is no following
    paragraph to merge into: there the previous paragraph's mark is the insertion and the new one
    keeps the original mark. Raises DocxError when the anchor cannot be copied that way."""
    ctx = Context(track, author, timestamp, id_source)
    last = after if after is not None else anchor
    paragraph = etree.Element(w("p"))
    if _followed_by_paragraph(last):
        props = _clean_paragraph_props(anchor, ctx, plain_body)
    elif not can_add_after(anchor, heading=plain_body):
        raise DocxError(UNSAFE_ADDITION)
    else:
        props = _carried_props(last, ctx)
    if len(props):
        paragraph.append(props)
    spans = text_spans(anchor)
    run_props = _props(spans[0].run) if spans else None
    run_props = None if run_props is None else _without_link_style(run_props)
    paragraph.append(_inserted(ctx, run_props, text))
    last.addnext(paragraph)
    return paragraph


def _without_link_style(props: etree._Element) -> etree._Element:
    clone = copy.deepcopy(props)
    style = clone.find(w("rStyle"))
    if style is not None and style.get(w("val")) == HYPERLINK_STYLE:
        clone.remove(style)
    return clone
