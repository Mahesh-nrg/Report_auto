"""The optional ``pdf2docx`` engine (layout-faithful; PyMuPDF is AGPL-3.0).

pdf2docx and fitz are imported inside :func:`run_pdf2docx` only, which the worker subprocess
calls; the gateway process never loads them. The DOCX it writes is post-processed: PDF font
names are reduced by ``strip_pdf_font_name`` and running header/footer paragraphs are removed
before section detection.
"""

from __future__ import annotations

import io
import logging
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from ..font_catalog_model import strip_pdf_font_name, valid_family
from .errors import PdfConversionFailed, PdfEngineUnavailable
from .extract import Page
from .lines import DIGITS, make_lines, running_keys
from .metrics import docx_counts, text_loss
from .model import ConversionMetrics
from .structure import EngineOutput

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
FONT_ATTRS = ("ascii", "hAnsi", "cs", "eastAsia")
QUIET_LOGGERS = ("pdf2docx", "fitz", "pymupdf", "root")
Converter = Callable[[Path, Path], None]


def _norm(text: str) -> str:
    return DIGITS.sub("#", " ".join(text.split())).casefold()


def running_texts(pages: Sequence[Page]) -> frozenset[str]:
    """Normalised texts of the PDF's running header/footer lines."""
    keys = running_keys(pages, [make_lines(page) for page in pages])
    return frozenset(text for _, text in keys)


def _fix_fonts(root) -> None:  # noqa: ANN001 - lxml element
    for fonts in root.iter(f"{W}rFonts"):
        for attr in FONT_ATTRS:
            raw = fonts.get(f"{W}{attr}")
            if raw is None:
                continue
            name = strip_pdf_font_name(raw)
            if valid_family(name) and name != raw:
                fonts.set(f"{W}{attr}", name)


def _segments(paragraph) -> list[list]:  # noqa: ANN001 - lxml element
    """The paragraph's runs split into visual lines at ``w:br`` (pdf2docx joins a PDF's lines
    with breaks)."""
    segments: list[list] = [[]]
    for run in paragraph.findall(f"{W}r"):
        segments[-1].append(run)
        if run.find(f"{W}br") is not None:
            segments.append([])
    return [segment for segment in segments if segment]


def _run_text(runs) -> str:  # noqa: ANN001 - lxml elements
    return "".join(node.text or "" for run in runs for node in run.iter(f"{W}t"))


def _drop_running(body, paragraph, running: frozenset[str]) -> list[str]:  # noqa: ANN001
    """Remove the running-line segments of one paragraph (the paragraph too when nothing is
    left); returns the removed texts."""
    if paragraph.find(f"{W}pPr/{W}sectPr") is not None:
        return []
    removed: list[str] = []
    for runs in _segments(paragraph):
        text = _run_text(runs)
        if text.strip() and _norm(text) in running:
            removed.append(text)
            for run in runs:
                paragraph.remove(run)
    if removed and not _run_text(paragraph.findall(f"{W}r")).strip():
        has_drawing = next(paragraph.iter(f"{W}drawing"), None) is not None
        if not has_drawing:
            body.remove(paragraph)
    return removed


def postprocess(data: bytes, running: frozenset[str]) -> tuple[bytes, tuple[str, ...]]:
    """Fonts normalised, running-line paragraphs dropped; returns the new DOCX and the texts
    removed (added back for the text-loss measure)."""
    import docx

    document = docx.Document(io.BytesIO(data))
    body = document.element.body
    _fix_fonts(body)
    for style in document.styles.element.iter(f"{W}style"):
        _fix_fonts(style)
    dropped: list[str] = []
    for paragraph in list(body.findall(f"{W}p")):
        dropped.extend(_drop_running(body, paragraph, running))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue(), tuple(dropped)


def _quiet() -> None:
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.CRITICAL)


def run_pdf2docx(pdf: Path, out: Path) -> None:
    """The real converter (lazy import; single process)."""
    try:
        from pdf2docx import Converter as Pdf2Docx
    except ImportError:
        raise PdfEngineUnavailable("pdf2docx") from None
    _quiet()
    converter = Pdf2Docx(str(pdf))
    try:
        converter.convert(str(out), multi_processing=False)
    finally:
        converter.close()


def convert_pdf2docx(
    pdf: Path,
    pages: Sequence[Page],
    work: Path,
    reference: str,
    converter: Converter = run_pdf2docx,
) -> EngineOutput:
    started = time.monotonic()
    out = work / "pdf2docx.docx"
    converter(pdf, out)
    try:
        raw = out.read_bytes()
    except OSError:
        raise PdfConversionFailed("bad_output") from None
    data, dropped = postprocess(raw, running_texts(pages))
    counts = docx_counts(data)
    lines = sum(len(make_lines(page)) for page in pages)
    metrics = ConversionMetrics(
        engine="pdf2docx",
        pages=len(pages),
        lines=lines,
        headings=counts.headings,
        title=counts.title,
        tables=counts.tables,
        images=counts.images,
        lists=counts.lists,
        text_loss=text_loss(reference, [*counts.texts, *dropped]) if reference else 0.0,
        dropped_running_lines=len(dropped),
        seconds=round(min(time.monotonic() - started, 3600.0), 2),
    )
    return EngineOutput(data, metrics, ("running_lines_removed",) if dropped else ())


__all__ = ["convert_pdf2docx", "postprocess", "run_pdf2docx", "running_texts"]
