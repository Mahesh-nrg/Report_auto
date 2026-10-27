"""Detect the sections and the block types of a DOCX (AI with a heuristic fallback).

Contract: docs/EXPORT_DESIGN.md (h). This package must stay importable without lxml: the reader
and detector modules are imported lazily by the services, never from here. Builders do not add
exports here; the integrator does.
"""

from __future__ import annotations

from . import errors, model
from .errors import LabelAnswerRejected, SectionsError
from .model import (
    Block,
    BlockLabel,
    DocumentSections,
    Section,
    build_sections,
    label_tool,
    merge_labels,
    parse_label_answer,
)

__all__ = [
    "Block",
    "BlockLabel",
    "DocumentSections",
    "LabelAnswerRejected",
    "Section",
    "SectionsError",
    "build_sections",
    "errors",
    "label_tool",
    "merge_labels",
    "model",
    "parse_label_answer",
]
