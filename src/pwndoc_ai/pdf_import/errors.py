"""User-safe errors of the PDF importer (fixed sentences, see ``export_errors``)."""

from __future__ import annotations

from ..export_errors import ExportLimitError, ExportRefused, ExportUnavailable, ExportUpstreamError

SCANNED_MESSAGE = (
    "This PDF has no text layer (scanned images). OCR is not installed on this server, so no"
    " sections can be detected. Install Tesseract, or upload a DOCX or a text-based PDF."
)


class PdfRefused(ExportRefused):
    """The PDF is refused before any engine runs, or it has nothing to convert (HTTP 422)."""

    default = "unreadable"
    MESSAGES = {
        "unreadable": "the PDF could not be read",
        "not_pdf": "the file is not a PDF",
        "encrypted": "encrypted or password-protected PDFs are not accepted",
        "javascript": "PDFs containing JavaScript are not accepted",
        "launch": "PDFs that launch programs are not accepted",
        "remote_action": "PDFs with actions that open other files are not accepted",
        "embedded_files": "PDFs with embedded or attached files are not accepted",
        "forms": "PDFs with active forms (XFA, submit or import actions) are not accepted",
        "media": "PDFs with embedded media are not accepted",
        "page_too_big": "the PDF has a page that is too large",
        "content_budget": "the PDF content is too large to analyse safely",
        "image_budget": "the PDF images are too large to analyse safely",
        "no_pages": "the PDF has no pages",
        "scanned": SCANNED_MESSAGE,
        "empty": "no text could be extracted from this PDF",
    }


class PdfEngineUnavailable(ExportUnavailable):
    """The chosen engine cannot run here (HTTP 501). ``pdf_tools``: mutool/pdftotext missing;
    ``pdf2docx``: the optional AGPL-licensed engine is not installed."""

    default = "pdf_tools"


class PdfConversionFailed(ExportUpstreamError):
    """The engine ran but did not produce a usable DOCX (HTTP 502)."""

    default = "failed"
    MESSAGES = {
        "failed": "the PDF conversion failed; see the gateway log",
        "timeout": "the PDF conversion took too long and was stopped",
        "crashed": "the PDF converter stopped unexpectedly (out of memory or a damaged file)",
        "bad_output": "the PDF converter produced a document that could not be used",
    }


__all__ = [
    "SCANNED_MESSAGE",
    "ExportLimitError",
    "PdfConversionFailed",
    "PdfEngineUnavailable",
    "PdfRefused",
]
