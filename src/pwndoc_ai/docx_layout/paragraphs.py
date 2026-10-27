"""Classify one body-level paragraph: text, objects, page breaks, numbering, headings.

Token string of a paragraph (document order): T text, O object-like content (drawing, field,
footnote reference, ...), L line break, B hard page break. Blank text and ignorable markup
produce no token.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lxml import etree

from .ooxml import local, ppr_props, w
from .styles import StyleIndex, outline_to_level

IGNORED_RUN_CHILDREN = frozenset({"rPr", "lastRenderedPageBreak", "annotationRef"})
TEXT_LIKE = frozenset({"tab", "noBreakHyphen", "softHyphen"})
OPAQUE = frozenset({"drawing", "pict", "object", "AlternateContent", "txbxContent"})
VISIBLE_RUN_MARKS = ("u", "strike", "dstrike", "highlight", "shd", "bdr")
CONTENT_TOKENS = frozenset("TOL")
OFF = frozenset({"none", "nil", "0", "false", "off"})


@dataclass(frozen=True)
class ParaFacts:
    text: str = ""
    tokens: str = ""
    lead: int = 0
    inner: int = 0
    trail: int = 0
    has_content: bool = False
    rich: bool = False
    decor: bool = False
    style_id: str | None = None
    pbb: bool = False
    pbb_direct: bool = False
    keep_next: bool = False
    heading: int | None = None
    caption: bool = False
    sect: etree._Element | None = field(default=None, compare=False, repr=False)
    bookmarks: tuple[str, ...] = ()
    unpaired: bool = False

    @property
    def brs(self) -> int:
        return self.tokens.count("B")

    @property
    def visible(self) -> bool:
        """Anything that renders: text, objects, bullets, borders, referenced bookmarks."""
        return self.has_content or self.decor


def _has_visible_mark(run: etree._Element) -> bool:
    rpr = run.find(w("rPr"))
    if rpr is None:
        return False
    for tag in VISIBLE_RUN_MARKS:
        node = rpr.find(w(tag))
        if node is not None and (node.get(w("val")) or "single").lower() not in OFF:
            return True
    return False


def _run_tokens(run: etree._Element) -> tuple[str, str]:
    tokens: list[str] = []
    text: list[str] = []
    for child in run:
        name = local(child)
        if name in IGNORED_RUN_CHILDREN:
            continue
        if name == "t":
            value = child.text or ""
            if value.strip():
                tokens.append("T")
                text.append(value)
            elif value and _has_visible_mark(run):
                tokens.append("O")
        elif name == "br":
            kind = child.get(w("type")) or "textWrapping"
            tokens.append("B" if kind == "page" else "L")
        elif name in TEXT_LIKE:
            tokens.append("T")
        else:
            tokens.append("O")
    return "".join(tokens), "".join(text)


def paragraph_text(element: etree._Element) -> str:
    """Visible text of a paragraph, without the text of text boxes."""
    parts: list[str] = []
    stack = [iter(element)]
    while stack:
        for child in stack[-1]:
            name = local(child)
            if name == "t":
                parts.append(child.text or "")
            elif name == "pPr" or name in OPAQUE:
                continue
            elif len(child):
                stack.append(iter(child))
                break
        else:
            stack.pop()
    return "".join(parts)


def _bookmarks(element: etree._Element) -> tuple[list[str], bool]:
    """Bookmark names in the paragraph and whether starts and ends pair up inside it."""
    starts = {c.get(w("id")): c.get(w("name")) or "" for c in element.findall(w("bookmarkStart"))}
    ends = {c.get(w("id")) for c in element.findall(w("bookmarkEnd"))}
    return list(starts.values()), set(starts) == ends


def _scan_children(element: etree._Element) -> tuple[str, str]:
    tokens: list[str] = []
    text: list[str] = []
    for child in element:
        name = local(child)
        if name == "r":
            run_tokens, run_text = _run_tokens(child)
            tokens.append(run_tokens)
            text.append(run_text)
        elif name not in ("pPr", "proofErr", "bookmarkStart", "bookmarkEnd"):
            tokens.append("O")
            text.append(paragraph_text(child) if len(child) else "")
    return "".join(tokens), "".join(text)


def _split_breaks(tokens: str) -> tuple[int, int, int]:
    """(breaks before the first content, breaks between content, breaks after the last)."""
    positions = [i for i, token in enumerate(tokens) if token in CONTENT_TOKENS]
    if not positions:
        return tokens.count("B"), 0, 0
    first, last = positions[0], positions[-1]
    return (
        tokens[:first].count("B"),
        tokens[first : last + 1].count("B"),
        tokens[last + 1 :].count("B"),
    )


def classify(
    element: etree._Element, styles: StyleIndex, referenced: frozenset[str] | set[str]
) -> ParaFacts:
    """Facts about a body-level paragraph. ``referenced`` holds the bookmark names in use."""
    ppr = element.find(w("pPr"))
    direct = ppr_props(ppr)
    style = ppr.find(w("pStyle")) if ppr is not None else None
    style_id = style.get(w("val")) if style is not None else None
    effective_id = style_id or styles.default_style("paragraph")
    tokens, text = _scan_children(element)
    names, paired = _bookmarks(element)
    lead, inner, trail = _split_breaks(tokens)
    locked = (not paired) or any(name in referenced for name in names)
    numbered = direct.get("numbered", styles.prop(effective_id, "numbered")) is True
    decor = bool(direct.get("decor") or styles.prop(effective_id, "decor")) or numbered
    outline = direct.get("outlineLvl", styles.prop(effective_id, "outlineLvl"))
    keep = direct.get("keepNext", styles.prop(effective_id, "keepNext"))
    pbb_style = styles.prop(effective_id, "pageBreakBefore")
    return ParaFacts(
        text=text,
        tokens=tokens,
        lead=lead,
        inner=inner,
        trail=trail,
        has_content=any(t in CONTENT_TOKENS for t in tokens) or locked,
        rich=any(t in "OL" for t in tokens),
        decor=decor,
        style_id=style_id,
        pbb=bool(direct.get("pageBreakBefore", pbb_style)),
        pbb_direct=bool(direct.get("pageBreakBefore")),
        keep_next=keep is True,
        heading=outline_to_level(outline, styles.heading_name_level(effective_id)),
        caption=styles.is_caption(effective_id),
        sect=ppr.find(w("sectPr")) if ppr is not None else None,
        bookmarks=tuple(names),
        unpaired=not paired,
    )
