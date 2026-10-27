"""Which PDF engines can run here. Uses ``shutil.which`` and ``find_spec`` only: pdf2docx and
PyMuPDF are never imported in the gateway process."""

from __future__ import annotations

import shutil
from importlib.util import find_spec

from .model import EngineInfo

REQUIRED_TOOLS = ("mutool", "pdftotext")
PDF2DOCX_MODULES = ("pdf2docx", "fitz")


def tools_present() -> bool:
    """mutool and pdftotext are on PATH (pdftoppm is optional: images are skipped without it)."""
    return all(shutil.which(tool) for tool in REQUIRED_TOOLS)


def _module_present(name: str) -> bool:
    try:
        return find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def pdf2docx_present() -> bool:
    return all(_module_present(name) for name in PDF2DOCX_MODULES)


def engines_available() -> tuple[EngineInfo, ...]:
    tools = tools_present()
    package = pdf2docx_present()
    pdf2docx_note = "" if tools and package else ("not_installed" if tools else "tools_missing")
    return (
        EngineInfo("structure", tools, "" if tools else "tools_missing"),
        EngineInfo("pdf2docx", tools and package, pdf2docx_note),
    )


__all__ = ["engines_available", "pdf2docx_present", "tools_present"]
