"""PDF -> DOCX worker, run in its own process by ``convert.py``:

    python -I .../pdf_import/worker.py --engine structure|pdf2docx in.pdf out.docx report.json

It prechecks the PDF again, reads it with mutool, refuses scanned PDFs, runs ONE engine and
writes ``report.json``: ``{"ok": true, "report": {...}}`` or ``{"ok": false, "error": kind,
"code": code}`` with a fixed error kind and code (never text from the document). Exit status 0
on success, 2 on a refusal or failure that was reported.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a script with -I: the package is found via site-packages
    try:
        import pwndoc_ai  # noqa: F401
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pwndoc_ai.export_errors import ExportFailure, ExportLimitError
from pwndoc_ai.pdf_import import limits
from pwndoc_ai.pdf_import.errors import PdfConversionFailed, PdfEngineUnavailable, PdfRefused
from pwndoc_ai.pdf_import.extract import Page, ToolError, load_pages
from pwndoc_ai.pdf_import.metrics import fonts_seen, pdftotext
from pwndoc_ai.pdf_import.model import WARNINGS, ConversionReport, PdfFacts, verdict_for
from pwndoc_ai.pdf_import.precheck import precheck_pdf
from pwndoc_ai.pdf_import.structure import EngineOutput, convert_structure
from pwndoc_ai.pdf_import.textlayer import has_text, scanned_share

ERROR_KINDS = {
    PdfRefused: "refused",
    ExportLimitError: "limit",
    PdfEngineUnavailable: "unavailable",
    PdfConversionFailed: "failed",
}
EXIT_OK, EXIT_REPORTED = 0, 2


def _engine(name: str, src: Path, pages: Sequence[Page], work: Path, text: str) -> EngineOutput:
    if name == "structure":
        return convert_structure(src, pages, work, text)
    from pwndoc_ai.pdf_import.pdf2docx_engine import convert_pdf2docx

    return convert_pdf2docx(src, pages, work, text)


def _fonts(pages: Sequence[Page]) -> tuple[str, ...]:
    counts: Counter = Counter()
    for page in pages:
        for span in page.spans:
            counts[span.font] += len(span.text)
    return fonts_seen(counts.items())


def _warnings(found: Sequence[str]) -> tuple[str, ...]:
    return tuple(code for code in dict.fromkeys(found) if code in WARNINGS)


def _report(
    engine: str, facts: PdfFacts, share: float, pages: Sequence[Page], output: EngineOutput
) -> ConversionReport:
    verdict = verdict_for(share)
    found = list(output.warnings)
    found += ["mixed_scanned"] if verdict == "mixed" else []
    found += ["no_headings"] if output.metrics.heading_total == 0 else []
    found += ["tagged_pdf"] if facts.tagged else []
    return ConversionReport(
        engine_requested=engine,
        engine_used=engine,
        reason="user_choice",
        verdict=verdict,
        scanned_share=share,
        facts=facts,
        metrics=output.metrics,
        fonts_seen=_fonts(pages),
        warnings=_warnings(found),
    )


def convert(engine: str, src: Path, out: Path) -> ConversionReport:
    """Precheck, text layer, one engine; writes ``out``. Raises the package errors."""
    facts = precheck_pdf(src, deadline=time.monotonic() + limits.PRECHECK_SECONDS)
    try:
        pages = load_pages(src, facts.pages)
    except ToolError:
        raise PdfConversionFailed("failed") from None
    if not pages:
        raise PdfRefused("no_pages")
    share = scanned_share(pages)
    if verdict_for(share) == "scanned":
        raise PdfRefused("scanned")
    if not has_text(pages):
        raise PdfRefused("empty")
    output = _engine(engine, src, pages, out.parent, pdftotext(src))
    if len(output.docx) > limits.MAX_OUTPUT_BYTES:
        raise PdfConversionFailed("bad_output")
    out.write_bytes(output.docx)
    return _report(engine, facts, share, pages, output)


def _error(exc: BaseException) -> dict[str, Any]:
    for kind_type, kind in ERROR_KINDS.items():
        if isinstance(exc, kind_type):
            return {"ok": False, "error": kind, "code": getattr(exc, "code", "failed")}
    return {"ok": False, "error": "failed", "code": "failed"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pdf_import.worker")
    parser.add_argument("--engine", choices=("structure", "pdf2docx"), required=True)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args(argv)
    try:
        report = convert(args.engine, args.source, args.output)
        payload: dict[str, Any] = {"ok": True, "report": report.to_json()}
        status = EXIT_OK
    except (ExportFailure, Exception) as exc:  # noqa: BLE001 - reported by a fixed kind only
        payload, status = _error(exc), EXIT_REPORTED
    args.report.write_text(json.dumps(payload), encoding="utf-8")
    return status


if __name__ == "__main__":
    sys.exit(main())
