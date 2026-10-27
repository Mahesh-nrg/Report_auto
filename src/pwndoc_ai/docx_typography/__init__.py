"""Apply a font plan (family and size per block type and per section) to a DOCX.

Contract: docs/EXPORT_DESIGN.md (i). This package must stay importable without lxml: the engine
modules (``apply`` and its helpers) are imported lazily by the services, never from here.
Builders do not add exports here; the integrator does.
"""

from __future__ import annotations

from . import errors, model
from .errors import TypographyError
from .model import (
    BlockAssignment,
    FontSpec,
    FontUse,
    SectionStyle,
    TypographyPlan,
    TypographyResult,
    plan_from_defaults,
)

__all__ = [
    "BlockAssignment",
    "FontSpec",
    "FontUse",
    "SectionStyle",
    "TypographyError",
    "TypographyPlan",
    "TypographyResult",
    "errors",
    "model",
    "plan_from_defaults",
]
