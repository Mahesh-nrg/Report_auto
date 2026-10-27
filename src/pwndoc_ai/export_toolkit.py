"""The engines the Export service drives, behind one seam (real ones lazily imported).

The service never imports the PDF importer, the section detector, the typography engine or the
font catalog at import time: :func:`default_toolkit` builds thin wrappers that import them on
first use, so the gateway starts without lxml, python-docx, pypdf or pdf2docx, and the tests
replace the whole toolkit (``tests/export_fakes.py``) without touching the engines.
"""

from __future__ import annotations

import importlib.util
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .export_errors import ExportUnavailable

PDF_TOOLS = ("mutool", "pdftotext")
PRECHECK_SECONDS = 15.0


@dataclass(frozen=True)
class Toolkit:
    """Callables the service uses; each mirrors one builder module's public function."""

    precheck_pdf: Callable[..., Any]  # (path, *, deadline) -> PdfFacts
    engines: Callable[[], tuple[Any, ...]]  # () -> tuple[EngineInfo, ...]
    convert_pdf: Callable[..., tuple[bytes, Any]]  # (src, work, engine, *, timeout)
    detect_sections: Callable[..., Any]  # (data, *, llm, masker, model, cancelled, progress)
    apply_plan: Callable[..., Any]  # (data, plan, assignments, *, seconds) -> TypographyResult
    fonts_used: Callable[[bytes], tuple[Any, ...]]  # (data) -> tuple[FontUse, ...]
    font_catalog: Callable[[Path], Any]  # (data_dir) -> FontCatalog


def _precheck_pdf(path: Path, *, deadline: float = PRECHECK_SECONDS) -> Any:
    """``deadline`` here is a duration in seconds; the importer wants a monotonic instant."""
    from .pdf_import.precheck import precheck_pdf

    return precheck_pdf(path, deadline=time.monotonic() + max(0.0, float(deadline)))


def _engines() -> tuple[Any, ...]:
    from .pdf_import.engines import engines_available

    return engines_available()


def _convert_pdf(src: Path, work: Path, engine: str = "auto", **kwargs: Any) -> tuple[bytes, Any]:
    from .pdf_import.convert import convert_pdf

    return convert_pdf(src, work, engine, **kwargs)


def _detect_sections(data: bytes, **kwargs: Any) -> Any:
    from .docx_sections.classify import detect_sections

    return detect_sections(data, **kwargs)


def _apply_plan(data: bytes, plan: Any, assignments: Any = None, **kwargs: Any) -> Any:
    from .docx_typography.apply import apply_plan

    return apply_plan(data, plan, assignments, **kwargs)


FONTS_USED_MODULES = ("scan", "apply", "fonts", "inspect")


def _fonts_used(data: bytes) -> tuple[Any, ...]:
    """``docx_typography.<module>.fonts_used`` (the contract does not pin the module)."""
    for name in FONTS_USED_MODULES:
        try:
            module = importlib.import_module(f"{__package__}.docx_typography.{name}")
        except ImportError:
            continue
        found = getattr(module, "fonts_used", None)
        if callable(found):
            return tuple(found(data))
    return ()


def _font_catalog(data_dir: Path) -> Any:
    from .font_catalog import font_catalog

    return font_catalog(data_dir)


def default_toolkit() -> Toolkit:
    """The production toolkit (every engine imported on first use)."""
    return Toolkit(
        precheck_pdf=_precheck_pdf,
        engines=_engines,
        convert_pdf=_convert_pdf,
        detect_sections=_detect_sections,
        apply_plan=_apply_plan,
        fonts_used=_fonts_used,
        font_catalog=_font_catalog,
    )


def pdf_tools_present() -> bool:
    """mutool and pdftotext are on PATH (the structure engine and the text-loss oracle)."""
    return all(shutil.which(tool) for tool in PDF_TOOLS)


@lru_cache(maxsize=1)
def pdf2docx_installed() -> bool:
    """The optional AGPL engine is importable (looked up without importing it)."""
    return all(importlib.util.find_spec(name) is not None for name in ("pdf2docx", "fitz"))


def require(missing: bool, code: str) -> None:
    if missing:
        raise ExportUnavailable(code)


__all__ = [
    "PRECHECK_SECONDS",
    "Toolkit",
    "default_toolkit",
    "pdf2docx_installed",
    "pdf_tools_present",
    "require",
]
