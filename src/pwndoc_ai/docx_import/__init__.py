"""Import a prefilled PwnDoc DOCX report into PwnDoc (company, audit, findings, screenshots).

Contract: docs/IMPORTER_DESIGN.md. This package must stay importable without lxml: modules that
parse XML (``reader`` and its helpers) are imported lazily by the jobs, never from here.
Builders do not add exports here; the integrator does.
"""

from __future__ import annotations

from . import errors, jsoncheck, limits, model, records
from .errors import (
    DocumentUnreadable,
    FallbackRejected,
    ImportCancelled,
    ImportConflict,
    ImportDenied,
    ImportEngineMissing,
    ImportFailure,
    ImportInputError,
    ImportLimitError,
    ImportNotFound,
    ImportSessionExpired,
    ImportUpstreamError,
)
from .model import (
    AnalyseRequest,
    AuditHeader,
    CompletenessIssue,
    Contact,
    CreatedObject,
    CreateRequest,
    HeaderFields,
    ImageBlob,
    ImportedFinding,
    ImportedImage,
    ImportedReport,
    ImportPlan,
    ImportResult,
    ImportStatus,
    Journal,
    NameCheck,
    PlanDefaults,
    PlanEdits,
    PocBlock,
    PwnDocOptions,
    SourceStats,
    UndoItem,
    UndoResult,
)

__all__ = [
    "AnalyseRequest", "AuditHeader", "CompletenessIssue", "Contact", "CreateRequest",
    "CreatedObject", "DocumentUnreadable", "FallbackRejected", "HeaderFields", "ImageBlob",
    "ImportCancelled", "ImportConflict", "ImportDenied", "ImportEngineMissing", "ImportFailure",
    "ImportInputError", "ImportLimitError", "ImportNotFound", "ImportPlan", "ImportResult",
    "ImportSessionExpired", "ImportStatus", "ImportUpstreamError", "ImportedFinding",
    "ImportedImage", "ImportedReport", "Journal", "NameCheck", "PlanDefaults", "PlanEdits",
    "PocBlock", "PwnDocOptions",
    "SourceStats", "UndoItem", "UndoResult", "errors", "jsoncheck", "limits", "model", "records",
]  # fmt: skip
