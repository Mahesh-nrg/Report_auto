"""DOCX engine: inspect an existing Word report and refine its text in place."""

from __future__ import annotations

from . import limits
from .errors import DocxError, RefineCancelled
from .inspection import inspect_docx
from .model import (
    Change,
    Inspection,
    Progress,
    RefineRequest,
    RefineResult,
    Section,
    Unit,
)
from .refine import refine_docx

__all__ = [
    "Change",
    "DocxError",
    "Inspection",
    "Progress",
    "RefineCancelled",
    "RefineRequest",
    "RefineResult",
    "Section",
    "Unit",
    "inspect_docx",
    "refine_docx",
    "limits",
]
