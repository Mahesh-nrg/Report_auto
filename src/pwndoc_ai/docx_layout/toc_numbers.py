"""Keep the chapter numbers in the table of contents right after tidy removes a numbered heading.

Chapter numbers are automatic (a list level on the heading), but the TOC entry holds a literal
copy ("8.", "2.1"). Removing the heading "7." makes the next heading "7." while its entry still
says "8.". Before a section is removed this module simulates the heading counters with and
without the removed headings and lowers the literal number of every entry that is affected. Only
entries whose text starts with a plain dotted number that has as many parts as the heading's
list level are touched; anything else (roman, letters, other formats) stays as it is.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from lxml import etree

from .context import Analysis
from .ooxml import local, ppr_props, w
from .scan import Block

MAX_LEVELS = 9
NUMBER = re.compile(r"^(\s*)(\d+(?:\.\d+)*)(\.?)(\s*)$")
LEADING_NUMBER = re.compile(r"^(\s*)(\d+(?:\.\d+)*)(\.?)(?=\s+\S)")
SKIPPED = frozenset({"pPr", "rPr", "instrText", "p"})

Ref = tuple[str, int]  # (numId, ilvl)
Vector = tuple[int, ...]


def heading_ref(block: Block, analysis: Analysis) -> Ref | None:
    """The list (numId, level) that numbers a heading, or None when it is not numbered."""
    ppr = block.element.find(w("pPr"))
    own = ppr_props(ppr).get("numRef") if ppr is not None else None
    styles = analysis.doc.styles
    inherited = styles.prop(block.facts.style_id or styles.default_style("paragraph"), "numRef")
    ident = level = None
    for ref in (own, inherited):
        if isinstance(ref, tuple):
            ident = ident if ident is not None else ref[0]
            level = level if level is not None else ref[1]
    if not ident or ident == "0":
        return None
    return ident, min(max(level or 0, 0), MAX_LEVELS - 1)


def _vectors(refs: Iterable[tuple[int, Ref]]) -> dict[int, Vector]:
    """Counter vector (levels 0..own) of each heading index, one counter set per list."""
    counters: dict[str, list[int]] = {}
    found: dict[int, Vector] = {}
    for index, (ident, level) in refs:
        state = counters.setdefault(ident, [0] * MAX_LEVELS)
        state[level] += 1
        for deeper in range(level + 1, MAX_LEVELS):
            state[deeper] = 0
        found[index] = tuple(state[: level + 1])
    return found


def _entry_nodes(paragraph: etree._Element) -> list[tuple[str, etree._Element]]:
    """('t', w:t) and ('tab', w:tab) of a TOC entry in reading order (no properties)."""
    out: list[tuple[str, etree._Element]] = []
    stack = list(reversed(list(paragraph)))
    while stack:
        node = stack.pop()
        name = local(node)
        if not isinstance(node.tag, str) or name in SKIPPED:
            continue
        if name == "t":
            out.append(("t", node))
        elif name in ("tab", "ptab"):
            out.append(("tab", node))
        else:
            stack.extend(reversed(list(node)))
    return out


def _literal(paragraph: etree._Element) -> tuple[list[etree._Element], re.Match[str]] | None:
    """The text nodes that hold the entry's number and the match of that number."""
    nodes = _entry_nodes(paragraph)
    first_tab = next((i for i, (kind, _) in enumerate(nodes) if kind == "tab"), None)
    if first_tab is not None:
        texts = [node for kind, node in nodes[:first_tab] if kind == "t"]
        match = NUMBER.match("".join(node.text or "" for node in texts)) if texts else None
        if match and first_tab + 1 < len(nodes):
            return texts, match
    first = next((node for kind, node in nodes if kind == "t"), None)
    match = LEADING_NUMBER.match(first.text or "") if first is not None else None
    return ([first], match) if first is not None and match else None


def _shifted(paragraph: etree._Element, delta: Vector) -> bool:
    found = _literal(paragraph)
    if found is None:
        return False
    nodes, match = found
    parts = [int(part) for part in match.group(2).split(".")]
    if len(parts) != len(delta):
        return False
    new = [part - drop for part, drop in zip(parts, delta, strict=True)]
    if min(new) < 1:
        return False
    label = ".".join(str(part) for part in new) + match.group(3)
    first, *rest = nodes
    text = first.text or ""
    if len(nodes) == 1 and LEADING_NUMBER.match(text) and not NUMBER.match(text):
        first.text = text[: match.start(2)] + label + text[match.end(3) :]
    else:
        first.text = match.group(1) + label + match.group(4)
        for node in rest:
            node.text = ""
    return True


def renumber_entries(analysis: Analysis, removed: Iterable[Block]) -> int:
    """Lower the literal numbers of the TOC entries that follow the headings in ``removed``
    (call before the headings are deleted). Returns the number of entries changed."""
    gone = {id(block) for block in removed}
    toc_refs: Mapping[str, tuple[etree._Element, ...]] = analysis.fields.toc_refs
    if not gone or not toc_refs:
        return 0
    numbered: list[tuple[int, Ref]] = []
    for index, block in enumerate(analysis.blocks):
        ref = heading_ref(block, analysis) if block.is_heading else None
        if ref is not None:
            numbered.append((index, ref))
    if not any(id(analysis.blocks[i]) in gone for i, _ in numbered):
        return 0
    before = _vectors(numbered)
    after = _vectors((i, ref) for i, ref in numbered if id(analysis.blocks[i]) not in gone)
    changed = 0
    done: set[int] = set()
    for index, vector in after.items():
        drop = tuple(old - new for old, new in zip(before[index], vector, strict=True))
        if not any(drop):
            continue
        for name in analysis.blocks[index].facts.bookmarks:
            for paragraph in toc_refs.get(name, ()):
                if id(paragraph) not in done and _shifted(paragraph, drop):
                    done.add(id(paragraph))
                    changed += 1
    return changed
