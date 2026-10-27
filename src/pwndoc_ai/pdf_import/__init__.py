"""Convert an uploaded PDF into a DOCX (structure rebuild, or the optional pdf2docx engine).

Contract: docs/EXPORT_DESIGN.md (g). This package must stay importable without pypdf, lxml,
python-docx, pdf2docx or PyMuPDF: engines run in an isolated worker subprocess and are imported
there lazily, never from here. Builders do not add exports here; the integrator does.
"""

from __future__ import annotations

from . import errors, model
from .errors import PdfConversionFailed, PdfEngineUnavailable, PdfRefused
from .model import (
    ENGINES,
    ConversionMetrics,
    ConversionReport,
    EngineInfo,
    PdfFacts,
    engine_choice,
)

__all__ = [
    "ENGINES",
    "ConversionMetrics",
    "ConversionReport",
    "EngineInfo",
    "PdfConversionFailed",
    "PdfEngineUnavailable",
    "PdfFacts",
    "PdfRefused",
    "engine_choice",
    "errors",
    "model",
]
