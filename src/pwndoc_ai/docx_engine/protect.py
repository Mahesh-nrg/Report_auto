"""Which paragraphs the engine may not edit, and why (reasons are short and user-safe)."""

from __future__ import annotations

import re

from lxml import etree

from . import limits
from .runs import RPR, iter_runs, run_text
from .styles import StyleMap
from .xmlutil import MC_NS, attr, w

R_TAG, HYPERLINK, PPR = w("r"), w("hyperlink"), w("pPr")
BENIGN_PARAGRAPH_CHILDREN = frozenset(
    {w("pPr"), w("bookmarkStart"), w("bookmarkEnd"), w("proofErr"), w("r"), w("hyperlink")}
)
BENIGN_LINK_CHILDREN = frozenset({w("r"), w("bookmarkStart"), w("bookmarkEnd"), w("proofErr")})
PARAGRAPH_REASONS = {
    w("fldSimple"): "field",
    w("ins"): "tracked changes",
    w("del"): "tracked changes",
    w("moveFrom"): "tracked changes",
    w("moveTo"): "tracked changes",
    w("moveFromRangeStart"): "tracked changes",
    w("moveToRangeStart"): "tracked changes",
    w("commentRangeStart"): "comment",
    w("commentRangeEnd"): "comment",
    w("oMath"): "math",
    w("oMathPara"): "math",
    w("sdt"): "content control",
}
RUN_BENIGN = frozenset({w("rPr"), w("t"), w("tab"), w("br"), w("lastRenderedPageBreak")})
RUN_REASONS = {
    w("drawing"): "image or drawing",
    w("pict"): "image or drawing",
    w("object"): "image or drawing",
    f"{{{MC_NS}}}AlternateContent": "image or drawing",
    w("footnoteReference"): "footnote or endnote",
    w("endnoteReference"): "footnote or endnote",
    w("commentReference"): "comment",
    w("annotationRef"): "comment",
    w("fldChar"): "field",
    w("instrText"): "field",
    w("fldData"): "field",
    w("sym"): "symbol or special character",
    w("noBreakHyphen"): "symbol or special character",
    w("softHyphen"): "symbol or special character",
    w("delText"): "tracked changes",
    w("delInstrText"): "tracked changes",
}
TRACKED_IN_PROPS = frozenset(
    {w("ins"), w("del"), w("moveFrom"), w("moveTo"), w("rPrChange"), w("pPrChange")}
)
TOC_STYLE = re.compile(r"^(toc\s*\d*|toc heading|table of (contents|figures)|tof)$", re.IGNORECASE)
CODE_WORDS = ("code", "preformatted", "verbatim")
MONOSPACE_WORDS = ("mono", "courier", "consolas", "menlo", "monaco", "lucida console", "terminal")
LINE_BREAK_TYPES = (None, "textWrapping")


def _props_reason(element: etree._Element | None) -> str | None:
    if element is not None and any(child.tag in TRACKED_IN_PROPS for child in element):
        return "tracked changes"
    return None


def _run_reason(run: etree._Element) -> str | None:
    for child in run:
        tag = child.tag
        if not isinstance(tag, str) or tag == w("rPr"):
            continue
        if tag in RUN_REASONS:
            return RUN_REASONS[tag]
        if tag == w("br") and attr(child, "type") not in LINE_BREAK_TYPES:
            return "page break"
        if tag not in RUN_BENIGN:
            return "unsupported content"
    return _props_reason(run.find(RPR))


def _link_reason(link: etree._Element) -> str | None:
    for child in link:
        if not isinstance(child.tag, str):
            continue
        if child.tag not in BENIGN_LINK_CHILDREN:
            return PARAGRAPH_REASONS.get(child.tag, "unsupported content")
        if child.tag == R_TAG and (reason := _run_reason(child)):
            return reason
    return None


def _child_reason(child: etree._Element) -> str | None:
    tag = child.tag
    if tag == PPR:
        return _props_reason(child.find(RPR)) or _props_reason(child)
    if tag == R_TAG:
        return _run_reason(child)
    if tag == HYPERLINK:
        return _link_reason(child)
    if tag in BENIGN_PARAGRAPH_CHILDREN:
        return None
    return PARAGRAPH_REASONS.get(tag, "unsupported content")


def structural_reason(paragraph: etree._Element) -> str | None:
    """The first reason the paragraph content cannot be rewritten safely, from its XML alone."""
    for child in paragraph:
        if isinstance(child.tag, str) and (reason := _child_reason(child)):
            return reason
    return None


def ancestor_reason(paragraph: etree._Element) -> str | None:
    for ancestor in paragraph.iterancestors():
        if ancestor.tag == w("txbxContent"):
            return "text box"
        if ancestor.tag == w("sdt"):
            return "content control"
        if ancestor.tag in (w("ins"), w("del"), w("moveFrom"), w("moveTo")):
            return "tracked changes"
    return None


def _is_monospace(font: str | None) -> bool:
    return bool(font) and any(word in font.lower() for word in MONOSPACE_WORDS)


def _run_font(run: etree._Element, style_font: str | None) -> str | None:
    props = run.find(RPR)
    fonts = props.find(w("rFonts")) if props is not None else None
    if fonts is not None:
        return attr(fonts, "ascii") or attr(fonts, "hAnsi") or style_font
    return style_font


def is_monospace_only(paragraph: etree._Element, style_font: str | None) -> bool:
    """Every run with visible text is set in a monospace font (and there is at least one)."""
    seen = False
    for run in iter_runs(paragraph):
        if not run_text(run).strip():
            continue
        if not _is_monospace(_run_font(run, style_font)):
            return False
        seen = True
    return seen


def _labels(style_id: str | None, styles: StyleMap) -> tuple[str, str]:
    return styles.name(style_id).lower(), (style_id or "").lower()


def toc_reason(style_id: str | None, styles: StyleMap) -> str | None:
    if any(TOC_STYLE.match(label) for label in _labels(style_id, styles)):
        return "table of contents"
    return None


def code_reason(
    paragraph: etree._Element, style_id: str | None, styles: StyleMap, text: str
) -> str | None:
    labels = _labels(style_id, styles)
    if any(word in label for label in labels for word in CODE_WORDS):
        return "code"
    if text.strip() and is_monospace_only(paragraph, styles.font(style_id)):
        return "code"
    return None


def protection_reason(
    paragraph: etree._Element, style_id: str | None, styles: StyleMap, text: str
) -> str | None:
    """Why this paragraph is protected, or None when it can be refined."""
    reason = (
        ancestor_reason(paragraph)
        or toc_reason(style_id, styles)
        or structural_reason(paragraph)
        or code_reason(paragraph, style_id, styles, text)
    )
    if reason is None and not text.strip():
        return "empty"
    if reason is None and len(text) > limits.MAX_REFINE_PARAGRAPH_CHARS:
        return "too long"
    return reason
