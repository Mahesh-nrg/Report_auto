"""Best-effort refresh of the page numbers in a Word table of contents.

A TOC is a complex field whose cached result (entries with a page number after the last tab)
was computed by Word, or by nobody: LibreOffice's headless conversion does not update it, so the
numbers in the PDF are wrong as soon as the layout differs. After a first render this module

* reads the TOC entries from the DOCX (``read_toc``),
* finds the page where each heading really starts in the PDF (``locate_headings``): a line of the
  heading text on a page after the TOC pages, in document order,
* maps the pages through the blank pages that will be removed (``remap_pages``), and
* rewrites only the cached number text in the DOCX (``apply_toc_pages``) for a second render.

Entry text, field codes, bookmarks, hyperlinks and every other part of the package stay as
they were. An entry is looked up under the text of the heading its link points to, which may have
been reworded since the TOC was built. Nothing here raises on odd input: an unreadable document,
a PDF it cannot read or a TOC it does not understand simply yields "no change". Documents that
restart or re-format page numbering (``w:pgNumType``) are left alone, because the PDF page is then
not the page number shown.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

MAX_PAGE_NUMBER = 9999
MAX_ENTRIES = 3000
MAX_WRAPPED_LINES = 4  # a heading or TOC entry that wraps over this many lines at most
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
MAX_LABEL_LENGTH = 300  # a heading longer than this is never copied into a TOC entry
MAX_FIELD_DEPTH = 64  # open fields at once; a deeper document has no TOC we trust
# '1.', '1.1', 'A.', 'IV.' before a space, or glued to the text ('1.Executive', '2.1Beta') as
# LibreOffice extracts auto-numbered headings; a bare number glued to a word ('3D') is kept.
NUMBERING = re.compile(
    r"^\s*(?:(?:\d+(?:\.\d+)*\.?|[A-Z]\.|[IVXLCDM]{1,6}\.)\s+"
    r"|(?:\d+(?:\.\d+)*\.|\d+\.\d+|[A-Z]\.|[IVXLCDM]{1,6}\.)(?=[^\W\d_]))"
)
PAGE_DIGITS = re.compile(r"[0-9]{1,5}")
WORDS = re.compile(r"\w+")


@dataclass(frozen=True)
class TocEntry:
    """A TOC entry: the normalised heading words and the page number the document caches."""

    title: str
    page: int


@dataclass
class _Slot:
    """An entry together with the text nodes that hold its number (internal, mutable)."""

    entry: TocEntry
    nodes: tuple[Any, ...]
    label: tuple[Any, ...] = ()  # text nodes of the title (after the number, before the page)
    numbered: bool = False  # the number sits in its own part, before a tab of its own
    shown: str = ""  # the title words the entry shows now
    heading: str | None = None  # text of the heading the entry links to, when it differs


# -- titles ------------------------------
def _words(raw: str, *, strip_numbering: bool = True) -> tuple[str, ...]:
    """Case-folded words of a heading or line, without a leading '1.', '1.1', 'A.' or 'IV.'."""
    text = unicodedata.normalize("NFKC", raw)
    if strip_numbering:
        text = NUMBERING.sub("", text, count=1)
    return tuple(WORDS.findall(text.casefold()))


# -- reading the DOCX ------------------------------
def _load(docx: bytes) -> tuple[Any, Any] | None:
    """(package, parsed main part) or None when the document cannot be read safely."""
    try:
        from .docx_engine import limits
        from .docx_engine.errors import DocxError
        from .docx_engine.package import open_package
        from .docx_engine.xmlutil import parse_xml
    except ImportError:
        log.info("TOC refresh skipped: the DOCX engine is not available")
        return None
    try:
        package = open_package(docx)
        return package, parse_xml(package.read(package.main_part, limits.MAX_XML_BYTES))
    except DocxError:
        return None
    except Exception as exc:  # noqa: BLE001 - untrusted input: never fatal here
        log.info("TOC refresh skipped: document unreadable (%s)", type(exc).__name__)
        return None


def _w(tag: str) -> str:
    return f"{{http://schemas.openxmlformats.org/wordprocessingml/2006/main}}{tag}"


class _OpenFields:
    """The stack of open fields: [instruction, in result part, is a TOC result], with the number
    of open TOC results counted so that no element has to search the stack."""

    def __init__(self) -> None:
        self.stack: list[list[Any]] = []
        self.toc_results = 0

    def push(self, instruction: str, in_result: bool) -> bool:
        if len(self.stack) >= MAX_FIELD_DEPTH:
            return False
        self.stack.append([instruction, in_result, False])
        self._mark(self.stack[-1])
        return True

    def _mark(self, item: list[Any]) -> None:
        if item[1] and not item[2] and item[0].lstrip().upper().startswith("TOC"):
            item[2] = True
            self.toc_results += 1

    def pop(self) -> None:
        if self.stack:
            if self.stack.pop()[2]:
                self.toc_results -= 1

    def separate(self) -> None:
        if self.stack:
            self.stack[-1][1] = True
            self._mark(self.stack[-1])

    def add_instruction(self, text: str) -> None:
        if self.stack and not self.stack[-1][1]:
            self.stack[-1][0] += text

    @property
    def collecting(self) -> bool:
        return bool(self.stack) and not self.stack[-1][1]


def _toc_paragraphs(root: Any) -> list[Any]:
    """Paragraphs that lie inside the cached result of a TOC field, in document order."""
    from lxml import etree

    fields = _OpenFields()
    open_paragraphs: list[Any] = []
    found: dict[Any, None] = {}
    for event, element in etree.iterwalk(root, events=("start", "end")):
        tag = element.tag
        if event == "end":
            if tag == _w("p") and open_paragraphs:
                open_paragraphs.pop()
            elif tag == _w("fldSimple"):
                fields.pop()
            continue
        if tag == _w("p"):
            open_paragraphs.append(element)
        elif tag == _w("fldChar"):
            if not _field_char(fields, element.get(_w("fldCharType"), "")):
                return []  # fields nested deeper than any real document: no TOC is read
        elif tag == _w("instrText"):
            fields.add_instruction(element.text or "")
        elif tag == _w("fldSimple") and not fields.push(element.get(_w("instr"), ""), True):
            return []
        if open_paragraphs and fields.toc_results:
            found[open_paragraphs[-1]] = None
    return list(found)


def _field_char(fields: _OpenFields, kind: str) -> bool:
    """Apply a ``w:fldChar``; False when the field depth limit is exceeded."""
    if kind == "begin":
        return fields.push("", False)
    if kind == "separate":
        fields.separate()
    elif kind == "end":
        fields.pop()
    return True


def _tokens(paragraph: Any) -> list[tuple[str, Any]]:
    """('t', w:t) and ('tab', w:tab) in reading order; skips properties and nested paragraphs."""
    skipped = {_w("pPr"), _w("rPr"), _w("p"), _w("instrText")}
    out: list[tuple[str, Any]] = []
    pending = list(reversed(list(paragraph)))
    while pending:
        node = pending.pop()
        tag = node.tag
        if not isinstance(tag, str) or tag in skipped:
            continue
        if tag == _w("t"):
            out.append(("t", node))
        elif tag in (_w("tab"), _w("ptab")):
            out.append(("tab", node))
        else:
            pending.extend(reversed(list(node)))
    return out


def _parse_entry(tokens: list[tuple[str, Any]]) -> _Slot | None:
    """An entry is: title text, a tab, then nothing but the page-number digits."""
    tabs = [i for i, (kind, _) in enumerate(tokens) if kind == "tab"]
    if not tabs:
        return None
    nodes = tuple(node for kind, node in tokens[tabs[-1] + 1 :] if kind == "t")
    digits = "".join(node.text or "" for node in nodes).strip()
    if not nodes or not PAGE_DIGITS.fullmatch(digits):
        return None
    raw = " ".join((node.text or "") if kind == "t" else " " for kind, node in tokens[: tabs[-1]])
    words = _words(raw)
    if not words:
        return None
    label_nodes, numbered = _label_nodes(tokens[: tabs[-1]], tabs)
    return _Slot(TocEntry(" ".join(words), int(digits)), nodes, label_nodes, numbered)


def _label_nodes(
    before_page: list[tuple[str, Any]], tabs: list[int]
) -> tuple[tuple[Any, ...], bool]:
    """The title text nodes of an entry and whether its number is a part of its own."""
    numbered = len(tabs) > 1
    start = tabs[0] + 1 if numbered else 0
    return tuple(node for kind, node in before_page[start:] if kind == "t"), numbered


def _heading_words(root: Any) -> dict[str, tuple[str, ...]]:
    """Words of the paragraph that holds each ``_Toc`` bookmark: what the PDF shows as heading."""
    found: dict[str, tuple[str, ...]] = {}
    for mark in root.iter(_w("bookmarkStart")):
        name, paragraph = mark.get(_w("name")) or "", mark.getparent()
        if name.startswith("_Toc") and paragraph is not None and paragraph.tag == _w("p"):
            words = _words("".join(t.text or "" for t in paragraph.iter(_w("t"))))
            if words:
                found.setdefault(name, words)
    return found


def _linked_title(paragraph: Any, headings: dict[str, tuple[str, ...]]) -> str | None:
    return _linked(paragraph, headings, lambda words: " ".join(words))


def _linked(paragraph: Any, found: dict[str, Any], convert: Any) -> Any:
    for link in paragraph.iter(_w("hyperlink")):
        value = found.get(link.get(_w("anchor")) or "")
        if value:
            return convert(value)
    return None


def _heading_texts(root: Any) -> dict[str, str]:
    """Text of the paragraph that holds each ``_Toc`` bookmark (whitespace collapsed)."""
    found: dict[str, str] = {}
    for mark in root.iter(_w("bookmarkStart")):
        name, paragraph = mark.get(_w("name")) or "", mark.getparent()
        if name.startswith("_Toc") and paragraph is not None and paragraph.tag == _w("p"):
            text = " ".join("".join(t.text or "" for t in paragraph.iter(_w("t"))).split())
            if text and len(text) <= MAX_LABEL_LENGTH:
                found.setdefault(name, text)
    return found


def _slots(root: Any) -> list[_Slot]:
    if any(
        element.get(_w("start")) is not None or element.get(_w("fmt")) is not None
        for element in root.iter(_w("pgNumType"))
    ):
        return []
    slots = []
    headings = _heading_words(root)
    texts = _heading_texts(root)
    for paragraph in _toc_paragraphs(root):
        slot = _parse_entry(_tokens(paragraph))
        if slot is not None:
            slot.shown = slot.entry.title
            title = _linked_title(paragraph, headings)  # the heading may be reworded since
            if title:
                slot.entry = TocEntry(title, slot.entry.page)
                if title != slot.shown:
                    slot.heading = _linked(paragraph, texts, str)
            slots.append(slot)
        if len(slots) >= MAX_ENTRIES:
            break
    return slots


def read_toc(docx: bytes) -> tuple[TocEntry, ...]:
    """The TOC entries of the document (empty without a TOC, or when numbering is not 1..n)."""
    loaded = _load(docx)
    if loaded is None:
        return ()
    return tuple(slot.entry for slot in _slots(loaded[1]))


# -- finding headings in the PDF ------------------------------
def _page_lines(
    pdf: bytes, texts: Sequence[str | None] | None = None, deadline: float | None = None
) -> list[list[str]] | None:
    """The non-empty lines of every page. ``texts`` (from ``pdf_pages.extract_texts``) saves
    reading the PDF again; None when the PDF cannot be read or ``deadline`` passes."""
    if texts is None:
        from .pdf_pages import extract_texts

        texts = extract_texts(pdf, deadline)
        if texts is None:
            return None
    return [[line.strip() for line in (text or "").splitlines() if line.strip()] for text in texts]


def _title_matches(
    words: Sequence[tuple[str, ...]], head: tuple[str, ...], titles: set[tuple[str, ...]]
) -> dict[tuple[str, ...], str]:
    """{title: 'heading' | 'toc'} for the lines that start at ``head`` and the ones after it.

    'heading' when the lines spell exactly a title, 'toc' when they spell a title followed by a
    page number (a TOC line, dot leaders and all). The first reading of a title wins.
    """
    found: dict[tuple[str, ...], str] = {}
    accumulated = head
    for offset in range(MAX_WRAPPED_LINES):
        if offset:
            if offset - 1 >= len(words):
                break
            accumulated = accumulated + words[offset - 1]
        if accumulated in titles:
            found.setdefault(accumulated, "heading")
        if len(accumulated) > 1 and accumulated[-1].isdecimal() and accumulated[:-1] in titles:
            found.setdefault(accumulated[:-1], "toc")
    return found


def _scan(
    pages: list[list[str]], titles: set[tuple[str, ...]], deadline: float | None = None
) -> tuple[dict, set[int]] | None:
    """({title: [(page, line), ...]} of heading-like lines, pages that hold TOC lines); every
    line is looked up in the title set (no title is compared with every line). None when
    ``deadline`` passes first."""
    first_words = {title[0] for title in titles}
    hits: dict[tuple[str, ...], list[tuple[int, int]]] = {title: [] for title in titles}
    toc_pages: set[int] = set()
    for number, lines in enumerate(pages, start=1):
        if deadline is not None and time.monotonic() >= deadline:
            return None
        heads = [_words(line) for line in lines]
        plain = [_words(line, strip_numbering=False) for line in lines]
        for index, head in enumerate(heads):
            if not head or head[0] not in first_words:
                continue
            for title, kind in _title_matches(plain[index + 1 :], head, titles).items():
                if kind == "toc":
                    toc_pages.add(number)
                else:
                    hits[title].append((number, index))
    return hits, toc_pages


def locate_headings(
    pdf: bytes,
    entries: Sequence[TocEntry],
    *,
    texts: Sequence[str | None] | None = None,
    deadline: float | None = None,
) -> tuple[int | None, ...]:
    """The 1-based PDF page where each entry's heading starts, ``None`` when it is not found.

    Only lines after the last page that holds TOC lines count, and each entry must come after the
    one before it, so repeated names ("Description") are matched in order. ``texts`` are the page
    texts when the caller already has them; nothing is scanned past ``deadline`` (a
    ``time.monotonic()`` value): the answer is then "not found" for every entry.
    """
    unresolved: tuple[int | None, ...] = (None,) * len(entries)
    pages = _page_lines(pdf, texts, deadline)
    titles = {tuple(e.title.split()) for e in entries if e.title}
    if pages is None or not titles:
        return unresolved
    scanned = _scan(pages, titles, deadline)
    if scanned is None or not scanned[1]:
        return unresolved
    hits, toc_pages = scanned
    previous = (max(toc_pages), 10**9)
    found: list[int | None] = []
    for entry in entries:
        later = [h for h in hits.get(tuple(entry.title.split()), []) if h > previous]
        if later:
            previous = later[0]
        found.append(later[0][0] if later else None)
    return tuple(found)


def remap_pages(pages: Sequence[int | None], removed: Sequence[int]) -> tuple[int | None, ...]:
    """Page numbers after the 1-based ``removed`` pages are cut out of the PDF."""
    cut = sorted(removed)
    return tuple(
        None if page is None else page - sum(1 for gone in cut if gone < page) for page in pages
    )


# -- writing the numbers back ------------------------------
def apply_toc_pages(docx: bytes, pages: Sequence[int | None]) -> bytes | None:
    """The DOCX with the cached TOC numbers set to ``pages`` (one per entry, None keeps it).

    Returns None when nothing changes, the list does not match the entries or the document cannot
    be rewritten.
    """
    loaded = _load(docx)
    if loaded is None:
        return None
    package, root = loaded
    slots = _slots(root)
    if len(slots) != len(pages):
        return None
    relabelled = _set_labels(slots)
    if not _set_numbers(slots, pages) and not relabelled:
        return None
    from lxml import etree

    from .docx_engine.errors import DocxError
    from .docx_engine.package import write_package

    xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    try:
        return write_package(package, {package.main_part: xml})
    except DocxError:
        return None


def _set_labels(slots: list[_Slot]) -> bool:
    """Give the entries whose heading was reworded the heading's text (the number part stays)."""
    changed = False
    for slot in slots:
        if slot.heading is None or not slot.label:
            continue
        first, *rest = slot.label
        shown = "".join(node.text or "" for node in slot.label)
        prefix = "" if slot.numbered else _number_prefix(shown)
        text = _strip_number(slot.heading) if (prefix or slot.numbered) else slot.heading
        first.text = prefix + text
        if first.text != first.text.strip():
            first.set(XML_SPACE, "preserve")
        for node in rest:
            node.text = ""
        changed = True
    return changed


def _number_prefix(text: str) -> str:
    """The leading '1.' / '2.1' / 'A.' of an entry text with the space after it ('' if none)."""
    match = NUMBERING.match(text)
    return match.group(0) if match and match.group(0).strip() else ""


def _strip_number(text: str) -> str:
    return NUMBERING.sub("", text, count=1).strip() or text


def _set_numbers(slots: list[_Slot], pages: Sequence[int | None]) -> bool:
    changed = False
    for slot, page in zip(slots, pages, strict=True):
        if page is None or not 1 <= page <= MAX_PAGE_NUMBER or page == slot.entry.page:
            continue
        first, *rest = slot.nodes
        first.text = str(page)
        for node in rest:
            node.text = ""
        changed = True
    return changed


def refresh_toc_numbers(docx: bytes, pdf: bytes) -> bytes | None:
    """The DOCX with TOC numbers taken from where the headings sit in ``pdf``; None if unchanged."""
    entries = read_toc(docx)
    if not entries:
        return None
    return apply_toc_pages(docx, locate_headings(pdf, entries))
