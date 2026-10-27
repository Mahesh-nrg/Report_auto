"""WordprocessingML primitives of the reader: the accepted text of a paragraph, table grids,
Word numbering, relationships and a wall-clock budget.

Text is read as Word shows it with every tracked change accepted: ``w:ins``/``w:moveTo`` are
read, ``w:del``/``w:moveFrom`` and deleted table rows are skipped, hidden runs (``w:vanish``),
field instructions and deleted text are skipped, content controls and smart tags are read
through, and of an ``mc:AlternateContent`` only the first ``mc:Choice`` is read. Text boxes are
never part of a paragraph's text (``textbox_paragraphs`` reads them separately).

This module never imports lxml: it works on the elements ``docx_engine.xmlutil.parse_xml``
returns through ``.tag``, ``.get`` and iteration only.
"""

from __future__ import annotations

import contextvars
import posixpath
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from .errors import ImportLimitError
from .html import Para

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
V_NS = "urn:schemas-microsoft-com:vml"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

READ_SECONDS = 20.0  # one read_report call (read as reader_xml.READ_SECONDS at call time)
MAX_TABLE_DEPTH = 4  # nested tables deeper than this are not read
CHECK_EVERY = 256

_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "import_read_deadline", default=None
)
_ticks: contextvars.ContextVar[int] = contextvars.ContextVar("import_read_ticks", default=0)


def w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def wattr(element, name: str) -> str | None:
    return element.get(w(name))


T_P, T_TBL, T_TR, T_TC, T_R = w("p"), w("tbl"), w("tr"), w("tc"), w("r")
T_T, T_TAB, T_PTAB, T_BR, T_CR = w("t"), w("tab"), w("ptab"), w("br"), w("cr")
T_NBH, T_SDT, T_SDT_CONTENT, T_CUSTOM = (
    w("noBreakHyphen"),
    w("sdt"),
    w("sdtContent"),
    w("customXml"),
)
T_TXBX = w("txbxContent")
MC_ALT, MC_CHOICE, MC_FALLBACK = (
    f"{{{MC_NS}}}{t}" for t in ("AlternateContent", "Choice", "Fallback")
)
BLIP, IMAGEDATA = f"{{{A_NS}}}blip", f"{{{V_NS}}}imagedata"
EMBED, RID = f"{{{R_NS}}}embed", f"{{{R_NS}}}id"
SKIP = frozenset(
    w(t)
    for t in (
        "del", "moveFrom", "delText", "instrText", "delInstrText", "fldData", "rPr", "pPr",
        "sdtPr", "sdtEndPr", "rPrChange", "pPrChange", "customXmlPr", "smartTagPr", "sym",
        "footnoteReference", "endnoteReference", "commentReference", "annotationRef",
        "softHyphen", "txbxContent", "p", "tbl", "lastRenderedPageBreak",
    )
)  # fmt: skip
GRAPHICS = frozenset({w("drawing"), w("pict"), w("object")})
FALSE = frozenset({"0", "false", "off", "none"})


# ---------------------------------------------------------------- budget


@contextmanager
def budget(seconds: float | None = None) -> Iterator[None]:
    """Run the block under a wall-clock budget (default ``READ_SECONDS``)."""
    new = time.monotonic() + (READ_SECONDS if seconds is None else seconds)
    current = _deadline.get()
    token = _deadline.set(new if current is None else min(current, new))
    ticks = _ticks.set(0)
    try:
        yield
    finally:
        _deadline.reset(token)
        _ticks.reset(ticks)


def check() -> None:
    """Raise ``ImportLimitError("too_large")`` once the budget is spent (cheap to call often)."""
    count = _ticks.get() + 1
    _ticks.set(count)
    if count % CHECK_EVERY:
        return
    deadline = _deadline.get()
    if deadline is not None and time.monotonic() > deadline:
        raise ImportLimitError("too_large")


# ---------------------------------------------------------------- paragraphs


@dataclass(frozen=True)
class ImageRef:
    """A picture placed in the document: its relationship id and document position."""

    rid: str
    order: int


class Counter:
    """Hands out document positions."""

    def __init__(self) -> None:
        self.value = 0

    def next(self) -> int:
        self.value += 1
        return self.value


def _is_on(element) -> bool:
    return element is not None and (wattr(element, "val") or "true").lower() not in FALSE


def _run_flags(run) -> tuple[bool, bool]:
    """(hidden, bold) of a ``w:r``."""
    props = run.find(w("rPr"))
    if props is None:
        return False, False
    hidden = _is_on(props.find(w("vanish"))) or _is_on(props.find(w("specVanish")))
    return hidden, _is_on(props.find(w("b")))


def _graphic_refs(node, out: list, counter: Counter) -> None:
    """Images in a drawing/pict/object, reading only the first mc:Choice of alternatives."""
    for child in node:
        tag = child.tag
        if not isinstance(tag, str) or tag == MC_FALLBACK or tag == T_TXBX:
            continue
        if tag == MC_ALT:
            _alternate(child, lambda c: _graphic_refs(c, out, counter))
            continue
        rid = child.get(EMBED) if tag == BLIP else child.get(RID) if tag == IMAGEDATA else None
        if rid:
            out.append(ImageRef(rid, counter.next()))
        _graphic_refs(child, out, counter)


def _alternate(node, visit) -> None:
    choice = node.find(MC_CHOICE)
    target = choice if choice is not None else node.find(MC_FALLBACK)
    if target is not None:
        visit(target)


class _ParaState:
    def __init__(self) -> None:
        self.tokens: list = []  # str or ImageRef
        self.bold_all = True
        self.has_text = False


def _token(tag: str, child) -> str | None:
    if tag == T_T:
        return child.text or ""
    if tag in (T_TAB, T_PTAB):
        return "\t"
    if tag == T_BR:
        kind = wattr(child, "type")
        return "\n" if kind in (None, "textWrapping") else "\n\n"
    if tag == T_CR:
        return "\n"
    if tag == T_NBH:
        return "-"
    return None


def _walk(node, state: _ParaState, counter: Counter, bold: bool | None) -> None:
    for child in node:
        tag = child.tag
        if not isinstance(tag, str) or tag in SKIP:
            continue
        if tag == T_R:
            hidden, run_bold = _run_flags(child)
            if not hidden:
                _walk(child, state, counter, run_bold)
            continue
        text = _token(tag, child)
        if text is not None:
            state.tokens.append(text)
            if text.strip():
                state.has_text = True
                state.bold_all = state.bold_all and bool(bold)
        elif tag in GRAPHICS:
            _graphic_refs(child, state.tokens, counter)
        elif tag == MC_ALT:
            _alternate(child, lambda c: _walk(c, state, counter, bold))
        else:
            _walk(child, state, counter, bold)


def list_info(p, numbering: dict[str, dict[int, str]]) -> tuple[str, int]:
    """(``"ul"``/``"ol"``/``""``, level) from the paragraph's own ``w:numPr``."""
    props = p.find(w("pPr"))
    num = props.find(w("numPr")) if props is not None else None
    if num is None:
        return "", 0
    num_id_el, level_el = num.find(w("numId")), num.find(w("ilvl"))
    num_id = wattr(num_id_el, "val") if num_id_el is not None else None
    if not num_id or num_id == "0":
        return "", 0
    raw = wattr(level_el, "val") if level_el is not None else "0"
    level = int(raw) if raw and raw.isdigit() and len(raw) < 3 else 0
    fmt = numbering.get(num_id, {}).get(level, "decimal")
    if fmt == "none":
        return "", 0
    return ("ul" if fmt == "bullet" else "ol"), min(level, 8)


def num_id_of(p) -> str:
    """The Word list id of the paragraph's own ``w:numPr`` (``""`` when none)."""
    props = p.find(w("pPr"))
    num = props.find(w("numPr")) if props is not None else None
    num_id_el = num.find(w("numId")) if num is not None else None
    value = wattr(num_id_el, "val") if num_id_el is not None else None
    return value if value and value != "0" else ""


def read_paragraph(p, numbering: dict, counter: Counter) -> list:
    """The paragraph as ``Para`` pieces and ``ImageRef`` items in reading order (a picture
    inside a paragraph splits it). A blank paragraph yields one blank ``Para``."""
    check()
    state = _ParaState()
    _walk(p, state, counter, None)
    kind, level = list_info(p, numbering)
    num_id = num_id_of(p) if kind else ""
    bold = state.bold_all and state.has_text
    items: list = []
    text: list[str] = []
    for token in state.tokens:
        if isinstance(token, ImageRef):
            if "".join(text).strip():
                items.append(Para("".join(text), kind, level, bold, num_id))
            items.append(token)
            text = []
        else:
            text.append(token)
    joined = "".join(text)
    if joined.strip() or not items:
        listed = bool(joined.strip())
        items.append(Para(joined, kind if listed else "", level, bold, num_id if listed else ""))
    return items


def para_text(p, numbering: dict | None = None) -> str:
    """Plain accepted text of one paragraph (pictures ignored)."""
    pieces = read_paragraph(p, numbering or {}, Counter())
    return "".join(piece.text for piece in pieces if isinstance(piece, Para))


# ---------------------------------------------------------------- containers and tables


def _through(node, wanted: str) -> Iterator:
    """Children with tag ``wanted``, reading through content controls and custom XML."""
    for child in node:
        tag = child.tag
        if tag == wanted:
            yield child
        elif tag == T_SDT:
            content = child.find(T_SDT_CONTENT)
            if content is not None:
                yield from _through(content, wanted)
        elif tag in (T_CUSTOM, w("ins"), w("moveTo")):
            yield from _through(child, wanted)


def blocks(node) -> Iterator:
    """Paragraphs and tables of a body or cell in document order."""
    for child in node:
        check()
        tag = child.tag
        if tag in (T_P, T_TBL):
            yield child
        elif tag == T_SDT:
            content = child.find(T_SDT_CONTENT)
            if content is not None:
                yield from blocks(content)
        elif tag == T_CUSTOM:
            yield from blocks(child)


def _row_deleted(tr) -> bool:
    props = tr.find(w("trPr"))
    return props is not None and props.find(w("del")) is not None


def rows(tbl) -> list[list]:
    """The table as rows of ``w:tc`` cells (deleted rows skipped)."""
    grid: list[list] = []
    for tr in _through(tbl, T_TR):
        check()
        if not _row_deleted(tr):
            grid.append(list(_through(tr, T_TC)))
    return grid


def cell_items(tc, numbering: dict, counter: Counter, depth: int = 0) -> list:
    """``Para`` and ``ImageRef`` items of one cell; nested tables are read in place, row by
    row, down to ``MAX_TABLE_DEPTH``."""
    items: list = []
    for block in blocks(tc):
        if block.tag == T_P:
            items.extend(read_paragraph(block, numbering, counter))
        elif depth < MAX_TABLE_DEPTH:
            for row in rows(block):
                for cell in row:
                    items.extend(cell_items(cell, numbering, counter, depth + 1))
    return items


def cell_text(tc, numbering: dict | None = None) -> str:
    """Plain text of a cell, one line per paragraph."""
    items = cell_items(tc, numbering or {}, Counter())
    return "\n".join(item.text for item in items if isinstance(item, Para))


def tables_within(tbl, depth: int = 0) -> Iterator:
    """Tables nested inside ``tbl`` (not ``tbl`` itself), document order, depth-capped."""
    if depth >= MAX_TABLE_DEPTH:
        return
    for row in rows(tbl):
        for cell in row:
            for block in blocks(cell):
                if block.tag == T_TBL:
                    yield block
                    yield from tables_within(block, depth + 1)


def textbox_paragraphs(root) -> Iterator:
    """Paragraphs of text boxes, skipping the ``mc:Fallback`` copy of each."""
    for box in root.iter(T_TXBX):
        check()
        if any(a.tag == MC_FALLBACK for a in box.iterancestors()):
            continue
        yield from _through(box, T_P)


def all_paragraphs(root) -> Iterator:
    """Every ``w:p`` of a part (headers, footers), text boxes excluded."""
    for p in root.iter(T_P):
        check()
        if not any(a.tag == T_TXBX for a in p.iterancestors()):
            yield p


# ---------------------------------------------------------------- numbering and relationships


def numbering_map(root) -> dict[str, dict[int, str]]:
    """``numId -> {ilvl: numFmt}`` from ``word/numbering.xml`` (level overrides applied)."""
    abstract: dict[str, dict[int, str]] = {}
    for node in root.findall(w("abstractNum")):
        abstract[wattr(node, "abstractNumId") or ""] = _levels(node)
    result: dict[str, dict[int, str]] = {}
    for node in root.findall(w("num")):
        ref = node.find(w("abstractNumId"))
        levels = dict(abstract.get(wattr(ref, "val") or "", {})) if ref is not None else {}
        for override in node.findall(w("lvlOverride")):
            levels.update(_levels(override))
        result[wattr(node, "numId") or ""] = levels
    return result


def _levels(node) -> dict[int, str]:
    levels: dict[int, str] = {}
    for lvl in node.findall(w("lvl")):
        raw = wattr(lvl, "ilvl") or ""
        fmt = lvl.find(w("numFmt"))
        if raw.isdigit() and len(raw) < 3 and fmt is not None:
            levels[int(raw)] = (wattr(fmt, "val") or "decimal").strip()
    return levels


@dataclass(frozen=True)
class Relationship:
    kind: str  # last segment of the Type URI, e.g. "image" or "header"
    target: str  # package member name, "" when external or outside the package


def relationships(root, part: str, names: frozenset[str]) -> dict[str, Relationship]:
    """``rId -> Relationship`` of ``part``'s rels; external and unknown targets resolve to
    ``""``, so their bytes are never read."""
    base = posixpath.dirname(part)
    result: dict[str, Relationship] = {}
    for rel in root.iter(f"{{{PKG_REL_NS}}}Relationship"):
        rid, raw = rel.get("Id") or "", rel.get("Target") or ""
        kind = (rel.get("Type") or "").rstrip("/").rpartition("/")[2]
        target = ""
        if rel.get("TargetMode") != "External" and raw and "\\" not in raw:
            joined = raw.lstrip("/") if raw.startswith("/") else posixpath.join(base, raw)
            normal = posixpath.normpath(joined)
            target = normal if normal in names else ""
        if rid:
            result[rid] = Relationship(kind, target)
    return result


def rels_name(part: str) -> str:
    folder, _, name = part.rpartition("/")
    return f"{folder}/_rels/{name}.rels" if folder else f"_rels/{name}.rels"
