"""Blank-page removal on a PDF that LibreOffice produced.

A hard page break after a run of empty paragraphs makes Word and LibreOffice emit pages that carry
nothing but the running header and footer. This module finds those pages and drops them.

A page is blank when, once everything that repeats on at least 60 percent of the pages is taken
away (the running header, footer, page number, header logo, header rule), nothing is left: no text,
no picture, no form, no painted vector shape. Digits are ignored when lines are compared, so
"Page 7 of 18" and "Page 8 of 18" are the same running line. The first page is never removed.
Anything that cannot be analysed (a page too large to scan, an encrypted or damaged file, no
pypdf) counts as content, and the PDF is returned exactly as it came; failures are logged by
exception type only, never with document content.
"""

from __future__ import annotations

import io
import logging
import re
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

log = logging.getLogger(__name__)

MAX_PDF_BYTES = 256 * 1024 * 1024
MAX_PAGES = 2000
MAX_ANALYSED_PAGES = 400  # a longer PDF is not scanned for blank pages (cost grows per page)
MAX_PAGE_STREAM_BYTES = 16 * 1024 * 1024  # decoded content of one page (or one form)
MAX_TOTAL_STREAM_BYTES = 256 * 1024 * 1024  # decoded content of the whole file
MAX_FORM_DEPTH = 3
RUNNING_NUMERATOR, RUNNING_DENOMINATOR = 3, 5  # 60 percent of the pages
COORDINATE_DIGITS = 1  # shapes are compared at 0.1 point

PATH_OPERATORS = frozenset({b"m", b"l", b"c", b"v", b"y", b"re", b"h"})
PAINT_OPERATORS = frozenset({b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"sh"})
DIGITS = re.compile(r"\d+")


class _Unanalysable(Exception):  # noqa: N818 - internal control flow, never leaves the module
    """A page (or the whole file) is too large or too odd to judge; it is then kept."""


def _load_pypdf() -> Any:
    try:
        import pypdf
    except ImportError:
        return None
    return pypdf


def _open(data: bytes) -> Any:
    """A ``pypdf.PdfReader`` for ``data`` or None (and a log line) when it cannot be analysed."""
    pypdf = _load_pypdf()
    if pypdf is None:
        log.warning("blank-page removal skipped: pypdf is not installed")
        return None
    if not data.startswith(b"%PDF") or len(data) > MAX_PDF_BYTES:
        log.info("blank-page removal skipped: not a PDF or too large")
        return None
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            log.info("blank-page removal skipped: the PDF is encrypted")
            return None
        if len(reader.pages) > MAX_PAGES:
            log.info("blank-page removal skipped: too many pages")
            return None
    except Exception as exc:  # noqa: BLE001 - pypdf raises many types on damaged input
        log.warning("blank-page removal skipped: unreadable PDF (%s)", type(exc).__name__)
        return None
    return reader


# -- what a page shows ------------------------------
def _normalise_line(line: str) -> str:
    return DIGITS.sub("#", " ".join(line.split()))


def _round(operands: Iterable[Any]) -> tuple[Any, ...]:
    out = []
    for value in operands:
        try:
            out.append(round(float(value), COORDINATE_DIGITS))
        except (TypeError, ValueError):
            out.append(str(value)[:40])
    return tuple(out)


def _decoded(holder: Any, budget: list[int]) -> bytes:
    """The decoded bytes of a content stream or an array of them, within the size budgets."""
    resolved = holder.get_object()
    parts = resolved if isinstance(resolved, list) else [resolved]
    chunks = [part.get_object().get_data() for part in parts]
    size = sum(len(chunk) for chunk in chunks)
    budget[0] -= size
    if size > MAX_PAGE_STREAM_BYTES or budget[0] < 0:
        raise _Unanalysable
    return b"\n".join(chunks)


class _Scan:
    """Collects the visible items of one page: shapes, pictures and forms (text is read apart)."""

    def __init__(self, reader: Any, budget: list[int]) -> None:
        self.reader = reader
        self.budget = budget
        self.items: set[str] = set()

    def run(self, holder: Any, resources: Any, depth: int = 0) -> None:
        from pypdf.generic import ContentStream, DecodedStreamObject

        stream = DecodedStreamObject()
        stream.set_data(_decoded(holder, self.budget))
        content = ContentStream(stream, self.reader)
        path: list[tuple[Any, ...]] = []
        transforms: list[tuple[Any, ...]] = [()]
        for operands, operator in content.operations:
            if operator == b"q":
                transforms.append(transforms[-1])
            elif operator == b"Q" and len(transforms) > 1:
                transforms.pop()
            elif operator == b"cm":
                transforms[-1] = (*transforms[-1], _round(operands))
            elif operator in PATH_OPERATORS:
                path.append((operator, _round(operands)))
            elif operator in PAINT_OPERATORS:
                self.items.add(f"s:{hash((operator, tuple(path), transforms[-1]))}")
                path = []
            elif operator == b"n":
                path = []
            elif operator == b"Do":
                self._xobject(operands, resources, transforms[-1], depth)
            elif operator == b"INLINE IMAGE":
                self.items.add(f"i:{hash((str(operands)[:200], transforms[-1]))}")

    def _xobject(
        self, operands: Any, resources: Any, transform: tuple[Any, ...], depth: int
    ) -> None:
        try:
            name = operands[0]
            reference = resources["/XObject"][name]
            target = reference.get_object()
        except (KeyError, IndexError, TypeError, AttributeError):
            self.items.add(f"x:unresolved:{transform}")
            return
        identity = getattr(reference, "idnum", None) or str(name)
        self.items.add(f"x:{identity}:{hash(transform)}")
        if target.get("/Subtype") == "/Form" and depth < MAX_FORM_DEPTH:
            inner = target.get("/Resources", resources)
            self.run(
                target, inner.get_object() if hasattr(inner, "get_object") else inner, depth + 1
            )


def _expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def extract_texts(pdf: bytes, deadline: float | None = None) -> list[str | None] | None:
    """The text of every page (None for a page that cannot be read), extracted once so that the
    blank-page scan and the TOC refresh can share it. None when the PDF cannot be opened, has too
    many pages to analyse, or ``deadline`` (a ``time.monotonic()`` value) passes first."""
    reader = _open(pdf)
    if reader is None or len(reader.pages) > MAX_ANALYSED_PAGES:
        return None
    texts: list[str | None] = []
    for page in reader.pages:
        if _expired(deadline):
            log.info("PDF text extraction stopped: out of time")
            return None
        try:
            texts.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001 - a bad page has no text, the rest still count
            log.info("PDF text extraction: a page could not be read (%s)", type(exc).__name__)
            texts.append(None)
    return texts


def _page_items(
    reader: Any, page: Any, index: int, budget: list[int], text: str | None
) -> frozenset[str]:
    """Normalised text lines plus painted shapes, pictures and forms of one page.

    A page that cannot be judged yields a marker no other page has, so it is never blank.
    """
    try:
        if text is None:
            raise _Unanalysable
        items = {
            f"t:{line}" for line in (_normalise_line(raw) for raw in text.splitlines()) if line
        }
        contents = page.get("/Contents")
        if contents is not None:
            scan = _Scan(reader, budget)
            resources = page.get("/Resources")
            scan.run(contents, resources.get_object() if resources is not None else {})
            items |= scan.items
    except _Unanalysable:
        return frozenset({f"unanalysed:{index}"})
    except Exception as exc:  # noqa: BLE001 - a damaged page is kept, not fatal
        log.info("blank-page scan kept page %d: %s", index + 1, type(exc).__name__)
        return frozenset({f"unanalysed:{index}"})
    return frozenset(items)


def _running_items(per_page: Sequence[frozenset[str]]) -> frozenset[str]:
    """Items found on at least 60 percent of the pages (rounded up)."""
    needed = -(-RUNNING_NUMERATOR * len(per_page) // RUNNING_DENOMINATOR)
    counts = Counter(item for items in per_page for item in items)
    return frozenset(item for item, seen in counts.items() if seen >= needed)


def page_count(pdf: bytes) -> int:
    """Number of pages of ``pdf``, 0 when it cannot be read."""
    reader = _open(pdf)
    return 0 if reader is None else len(reader.pages)


def _page_texts(reader: Any, texts: Sequence[str | None] | None) -> Iterable[str | None]:
    if texts is not None and len(texts) == len(reader.pages):
        return texts
    return (_read_text(page) for page in reader.pages)


def _read_text(page: Any) -> str | None:
    try:
        return page.extract_text() or ""
    except Exception as exc:  # noqa: BLE001 - the page is then kept
        log.info("blank-page scan: a page could not be read (%s)", type(exc).__name__)
        return None


def find_blank_pages(
    pdf: bytes,
    *,
    deadline: float | None = None,
    texts: Sequence[str | None] | None = None,
) -> tuple[int, ...]:
    """1-based numbers of the pages with nothing but running header/footer content.

    ``texts`` are the page texts from ``extract_texts`` (read again when absent). When the scan
    runs past ``deadline`` or the PDF has more than ``MAX_ANALYSED_PAGES`` pages, no page counts
    as blank: the PDF is then left as it is.
    """
    reader = _open(pdf)
    if reader is None or len(reader.pages) < 2:
        return ()
    if len(reader.pages) > MAX_ANALYSED_PAGES:
        log.info("blank-page scan skipped: too many pages")
        return ()
    budget = [MAX_TOTAL_STREAM_BYTES]
    per_page: list[frozenset[str]] = []
    for index, (page, text) in enumerate(
        zip(reader.pages, _page_texts(reader, texts), strict=False)
    ):
        if _expired(deadline):
            log.info("blank-page scan stopped: out of time")
            return ()
        per_page.append(_page_items(reader, page, index, budget, text))
    running = _running_items(per_page)
    return tuple(
        index + 1 for index, items in enumerate(per_page) if index > 0 and items <= running
    )


# -- dropping pages ------------------------------
def _fit_of(destination: Any) -> Any:
    from pypdf.generic import Fit

    def number(value: Any) -> float | None:
        return float(value) if isinstance(value, (int, float)) else None

    if getattr(destination, "typ", None) == "/XYZ":
        return Fit.xyz(
            left=number(destination.left),
            top=number(destination.top),
            zoom=number(destination.zoom),
        )
    return Fit.fit()


def _copy_outline(
    writer: Any, reader: Any, items: Sequence[Any], mapping: dict[int, int], parent: Any = None
) -> None:
    """Re-create bookmarks to kept pages; the children of a dropped one move up a level."""
    last = parent
    for item in items:
        if isinstance(item, list):
            _copy_outline(writer, reader, item, mapping, last)
            continue
        try:
            target = mapping.get(reader.get_destination_page_number(item))
        except Exception:  # noqa: BLE001 - an unresolvable bookmark is dropped
            target = None
        if target is None:
            last = parent
            continue
        last = writer.add_outline_item(
            str(item.title or ""), target, parent=parent, fit=_fit_of(item)
        )


def drop_pages(pdf: bytes, pages: Sequence[int]) -> bytes:
    """``pdf`` without the 1-based ``pages``; bookmarks and links to them are dropped.

    Returns ``pdf`` itself when nothing is dropped or the file cannot be rewritten.
    """
    reader = _open(pdf)
    drop = {number - 1 for number in pages}
    if reader is None or not drop:
        return pdf
    keep = [index for index in range(len(reader.pages)) if index not in drop]
    if not keep:
        return pdf
    try:
        return _write_kept(reader, keep)
    except Exception as exc:  # noqa: BLE001 - never lose the original on a rewrite problem
        log.warning(
            "dropping blank pages failed (%s); the PDF is left as rendered", type(exc).__name__
        )
        return pdf


def _write_kept(reader: Any, keep: list[int]) -> bytes:
    pypdf = _load_pypdf()
    writer = pypdf.PdfWriter()
    writer.append(reader, pages=keep, import_outline=False)
    mapping = {old: new for new, old in enumerate(keep)}
    _copy_outline(writer, reader, reader.outline, mapping)
    if reader.metadata:
        writer.add_metadata({key: str(value) for key, value in reader.metadata.items()})
    buffer = io.BytesIO()
    writer.write(buffer)
    produced = buffer.getvalue()
    if len(pypdf.PdfReader(io.BytesIO(produced)).pages) != len(keep):
        raise ValueError("page count mismatch")
    return produced


def remove_blank_pages(
    pdf: bytes,
    *,
    blanks: Sequence[int] | None = None,
    deadline: float | None = None,
    texts: Sequence[str | None] | None = None,
) -> tuple[bytes, tuple[int, ...]]:
    """``(pdf without its blank pages, their 1-based numbers in the input)``.

    ``blanks`` are the pages already found by ``find_blank_pages`` for this very PDF (the scan is
    not repeated). The input comes back byte for byte, with ``()``, when nothing is blank or on
    any problem.
    """
    if blanks is None:
        blanks = find_blank_pages(pdf, deadline=deadline, texts=texts)
    blanks = tuple(blanks)
    if not blanks:
        return pdf, ()
    cleaned = drop_pages(pdf, blanks)
    if cleaned is pdf:
        return pdf, ()
    return cleaned, blanks
