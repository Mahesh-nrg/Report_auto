"""The admin's default fonts (Settings) applied to PwnDoc audit downloads and refined copies.

Contract: docs/EXPORT_DESIGN.md (d) and (e). :func:`apply_defaults` never raises: a download must
not fail because a font could not be changed, so any error leaves the bytes as they were and
answers ``X-Typography: 0``. The engine (``docx_typography.apply``) is imported lazily, so this
module stays importable without lxml or python-docx.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .docx_typography.model import TypographyPlan, plan_from_defaults
from .export_errors import ExportFailure

log = logging.getLogger(__name__)

TYPOGRAPHY_OFF = {"X-Typography": "0"}
DOWNLOAD_SECONDS = 20.0

ApplyPlan = Callable[..., Any]


def settings_plan(settings: Any) -> TypographyPlan:
    """The plan for the typography settings; an empty plan when nothing is set (or invalid)."""
    try:
        return plan_from_defaults(
            default_font=str(getattr(settings, "default_font", "") or ""),
            default_size=float(getattr(settings, "default_font_size", 0.0) or 0.0),
            heading_font=str(getattr(settings, "heading_font", "") or ""),
            heading_size=float(getattr(settings, "heading_font_size", 0.0) or 0.0),
            mono_font=str(getattr(settings, "mono_font", "") or ""),
            mono_size=float(getattr(settings, "mono_font_size", 0.0) or 0.0),
        )
    except (ExportFailure, TypeError, ValueError) as exc:
        log.warning("typography settings ignored (%s)", type(exc).__name__)
        return TypographyPlan()


def load_apply_plan() -> ApplyPlan | None:
    """``docx_typography.apply.apply_plan``, or ``None`` when the engine cannot be imported."""
    try:
        from .docx_typography.apply import apply_plan
    except ImportError:
        return None
    return apply_plan


def apply_plan_to(data: bytes, plan: TypographyPlan) -> tuple[bytes, dict[str, str]]:
    """``plan`` applied to ``data`` (no assignments: block types are inferred by the engine)."""
    if plan.is_empty:
        return data, dict(TYPOGRAPHY_OFF)
    apply = load_apply_plan()
    if apply is None:
        return data, dict(TYPOGRAPHY_OFF)
    try:
        result = apply(data, plan, None, seconds=DOWNLOAD_SECONDS)
        output = getattr(result, "data", None)
        if not isinstance(output, bytes) or not output:
            raise ValueError("typography produced no document")
        return output, dict(result.headers())
    except Exception as exc:  # noqa: BLE001 - a hostile document must never break a download
        log.warning("default fonts not applied (%s)", type(exc).__name__)
        return data, dict(TYPOGRAPHY_OFF)


def apply_defaults(data: bytes, settings: Any) -> tuple[bytes, dict[str, str]]:
    """The admin's default fonts applied to a downloaded DOCX. Never raises."""
    return apply_plan_to(data, settings_plan(settings))


def typography_key(settings: Any) -> str:
    """A short cache-key part that changes whenever the typography settings change."""
    plan = settings_plan(settings)
    return "" if plan.is_empty else f"typ:{plan.digest()}"


__all__ = [
    "TYPOGRAPHY_OFF",
    "apply_defaults",
    "apply_plan_to",
    "load_apply_plan",
    "settings_plan",
    "typography_key",
]
