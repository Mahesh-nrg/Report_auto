"""Service glue between the download routes and the layout validator / PDF renderer.

The Word and PDF downloads are tidied by default: ``docx_layout`` finds and fixes blank pages,
orphan headings and runs of empty paragraphs, and ``pdf.convert_to_pdf_report`` renders with
metric-compatible fonts, removes blank pages and refreshes the table of contents. This module is
the one place the routes call. It never lets a layout problem fail a download: the validator is
imported lazily (the gateway runs without it), every failure is logged by exception type only (a
document's text or path never reaches a log or a response) and the untidied file is served with a
short fixed note. The CPU work is bounded by a global slot count and a per-user throttle.
"""

from __future__ import annotations

import importlib
import logging
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import pdf as pdf_module
from . import pdf_fonts
from .api_deps import limited
from .gateway import Gateway
from .reports import is_docx
from .throttle import Throttle

log = logging.getLogger(__name__)

MAX_PARALLEL = 2  # tidy / inspect runs at once across all users (CPU bound)
DOWNLOAD_WAIT = 30.0  # a download waits this long for a slot, then is served untidied
CHECK_WAIT = 5.0  # a check waits this long for a slot, then answers 503
RETRY_AFTER = "5"
MAX_TEXT = 200
FONT_LOOKUP_SECONDS = 15.0  # all fc-match lookups of one check together
MAX_HEADER_NUMBER = 999_999
LIMIT_NAME = "layout"
REQUIRED_FUNCTIONS = ("inspect_layout", "tidy_docx")
SEVERITIES = frozenset({"error", "warning", "info"})
STAT_KEYS = (
    "paragraphs", "empty_paragraphs", "longest_empty_run", "page_breaks", "section_breaks",
    "empty_sections", "orphan_headings", "est_blank_pages",
)  # fmt: skip
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
PDF_MAGIC = b"%PDF"
SKIPPED_SUFFIX = "_skipped"
NOT_INSTALLED = "The layout check is not available on this gateway."
UNREADABLE = "The layout of this document could not be checked."
FAILED = "The layout could not be checked right now; the file is unchanged."
BUSY = "The layout check is busy; the file is unchanged."
BAD_TIDY = "tidy must be 0 or 1"

_slots = threading.BoundedSemaphore(MAX_PARALLEL)

__all__ = [
    "HTTPException", "PdfOutcome", "Tidied", "check_payload", "layout_headers", "layout_limited",
    "load_layout", "parse_tidy", "render_pdf", "tidy_for_download",
]  # fmt: skip


class _Busy(RuntimeError):
    """No tidy / inspect slot became free in time."""


@dataclass(frozen=True)
class Tidied:
    """The bytes to serve, what was fixed (JSON-ready) and, when tidy could not run, why."""

    data: bytes
    fixes: tuple[dict[str, Any], ...] = ()
    fix_count: int = 0
    note: str = ""


@dataclass(frozen=True)
class PdfOutcome:
    """A rendered PDF and what is known about it (None: the renderer did not say)."""

    data: bytes
    pages: int | None = None
    removed_blank: int | None = None
    toc_updated: bool | None = None
    fix_count: int = 0

    def facts(self) -> dict[str, Any]:
        """Everything but the bytes: kept next to a cached PDF so a repeat download has headers."""
        return {k: v for k, v in asdict(self).items() if k != "data"}


# -- plumbing --------------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _import_layout() -> Any | None:
    try:
        module = importlib.import_module(".docx_layout", __package__)
    except ImportError as exc:
        log.warning("the layout validator is not available (%s)", type(exc).__name__)
        return None
    if not all(callable(getattr(module, name, None)) for name in REQUIRED_FUNCTIONS):
        log.warning("the layout validator is incomplete: no %s", " / ".join(REQUIRED_FUNCTIONS))
        return None
    return module


def load_layout() -> Any | None:
    """The ``docx_layout`` package, or None when it cannot be imported (looked up once)."""
    return _import_layout()


def parse_tidy(raw: str) -> bool:
    """The ``tidy`` query parameter: ``1`` (default) tidies, ``0`` opts out."""
    if raw not in ("0", "1"):
        raise HTTPException(400, BAD_TIDY)
    return raw == "1"


@contextmanager
def _slot(wait: float) -> Iterator[None]:
    if not _slots.acquire(timeout=wait):
        raise _Busy
    try:
        yield
    finally:
        _slots.release()


@contextmanager
def layout_limited(gw: Gateway, key: str) -> Iterator[None]:
    """Hold the per-user layout throttle (429 over the limit); created on first use."""
    gw.limits.setdefault(LIMIT_NAME, Throttle(in_flight=2, per_minute=60, max_total=6))
    with limited(gw, LIMIT_NAME, key):
        yield


def _text(value: Any) -> str:
    return CONTROL.sub(" ", "" if value is None else str(value)).strip()[:MAX_TEXT]


def _whole(value: Any, *, minimum: int = 0) -> int | None:
    ok = isinstance(value, int) and not isinstance(value, bool) and value >= minimum
    return value if ok else None


def _issue(item: Any) -> dict[str, Any]:
    severity = getattr(item, "severity", "")
    return {
        "code": _text(getattr(item, "code", "")),
        "severity": severity if isinstance(severity, str) and severity in SEVERITIES else "info",
        "message": _text(getattr(item, "message", "")),
        "fixable": getattr(item, "fixable", False) is True,
        "count": _whole(getattr(item, "count", 1), minimum=1) or 1,
        "where": _text(getattr(item, "where", "")),
    }


def _font(item: Any) -> dict[str, Any]:
    return {
        "font": _text(getattr(item, "font", "")),
        "resolved": _text(getattr(item, "resolved", "")),
        "compatible": getattr(item, "compatible", False) is True,
        "advice": _text(getattr(item, "advice", "")),
    }


def _stats(raw: Any) -> dict[str, int]:
    source = raw if isinstance(raw, Mapping) else {}
    return {k: v for k in STAT_KEYS if (v := _whole(source.get(k))) is not None}


def _fix_total(fixes: tuple[dict[str, Any], ...]) -> int:
    """How much was changed: a ``*_skipped`` note says that something was NOT touched."""
    return sum(f["count"] for f in fixes if not f["code"].endswith(SKIPPED_SUFFIX))


def _hostile(layout: Any) -> type[BaseException]:
    error = getattr(layout, "DocxLayoutError", ValueError)
    return error if isinstance(error, type) and issubclass(error, BaseException) else ValueError


# -- tidy at download time -------------------------------------------------------------------
def _accepted(docx: bytes, result: Any) -> Tidied:
    data = getattr(result, "data", None)
    if not is_docx(data):
        log.warning("the layout tidy returned no Word file; serving the original")
        return Tidied(docx, note=FAILED)
    if data == docx:
        return Tidied(docx)
    fixes = tuple(_issue(f) for f in getattr(result, "fixes", ()) or ())
    return Tidied(data, fixes, _fix_total(fixes))


def tidy_for_download(docx: bytes, *, enabled: bool = True) -> Tidied:
    """The document tidied for download. Never raises: on any problem ``docx`` comes back as is."""
    if not enabled:
        return Tidied(docx)
    layout = load_layout()
    if layout is None:
        return Tidied(docx, note=NOT_INSTALLED)
    try:
        with _slot(DOWNLOAD_WAIT):
            result = layout.tidy_docx(docx)
    except _Busy:
        return Tidied(docx, note=BUSY)
    except _hostile(layout):
        return Tidied(docx, note=UNREADABLE)
    except Exception as exc:  # noqa: BLE001 - a layout problem must never fail a download
        log.warning("the layout tidy failed (%s)", type(exc).__name__)
        return Tidied(docx, note=FAILED)
    return _accepted(docx, result)


# -- the check -------------------------------------------------------------------------------
def _empty_payload(*, available: bool, note: str) -> dict[str, Any]:
    return {
        "available": available, "note": note, "issues": [], "fonts": [], "stats": {},
        "tidied": {"fixes": [], "removed_blank_pages": 0},
    }  # fmt: skip


def _estimated_blank(report: Any) -> int:
    stats = _stats(getattr(report, "stats", None))
    return stats.get("est_blank_pages", 0)


@contextmanager
def _render_fonts() -> Iterator[Callable[[str], str | None] | None]:
    """A font matcher that answers as the PDF renderer will (Calibri and Aptos come out as
    Carlito, not as whatever the system default is), or None to use the system's own."""
    with tempfile.TemporaryDirectory(prefix="pwndoc-fonts-") as work:
        try:
            conf = pdf_fonts.write_fontconfig(Path(work))
            deadline = time.monotonic() + FONT_LOOKUP_SECONDS
            resolver: Callable[[str], str | None] | None = pdf_fonts.fc_matcher(conf, deadline)
        except OSError as exc:
            log.info("render font setup failed (%s); using the system fonts", type(exc).__name__)
            resolver = None
        yield resolver


def _inspect(layout: Any, docx: bytes, *, tidy: bool) -> dict[str, Any]:
    fixes: tuple[dict[str, Any], ...] = ()
    removed = 0
    with _render_fonts() as resolver:
        if tidy:
            result = layout.tidy_docx(docx, font_resolver=resolver)
            before = result.report_before
            fixes = tuple(_issue(f) for f in result.fixes or ())
            removed = max(0, _estimated_blank(before) - _estimated_blank(result.report_after))
        else:
            before = layout.inspect_layout(docx, font_resolver=resolver)
    return {
        **_empty_payload(available=True, note=""),
        "issues": [_issue(i) for i in before.issues or ()],
        "fonts": [_font(f) for f in before.fonts or ()],
        "stats": _stats(before.stats),
        "tidied": {"fixes": list(fixes), "removed_blank_pages": removed},
    }


@contextmanager
def _check_slot() -> Iterator[None]:
    try:
        with _slot(CHECK_WAIT):
            yield
    except _Busy as exc:
        raise HTTPException(503, BUSY, headers={"Retry-After": RETRY_AFTER}) from exc


def check_payload(docx: bytes, *, tidy: bool) -> dict[str, Any]:
    """What the validator finds in ``docx`` as it would be downloaded WITHOUT tidy, plus what the
    tidy would change (``tidy`` False for files that are never tidied). 200-shaped always; only a
    busy server is an error (503)."""
    layout = load_layout()
    if layout is None:
        return _empty_payload(available=False, note=NOT_INSTALLED)
    with _check_slot():
        try:
            return _inspect(layout, docx, tidy=tidy)
        except _hostile(layout):
            return _empty_payload(available=True, note=UNREADABLE)
        except Exception as exc:  # noqa: BLE001 - the check is advice, never a failure
            log.warning("the layout check failed (%s)", type(exc).__name__)
            return _empty_payload(available=True, note=FAILED)


# -- the PDF ---------------------------------------------------------------------------------
def _tidy_total(result: Any) -> int:
    fixes = tuple(_issue(f) for f in getattr(result, "tidy_fixes", ()) or ())
    return _fix_total(fixes)


def _outcome(result: Any) -> PdfOutcome:
    data = getattr(result, "data", None)
    if not isinstance(data, bytes) or not data.startswith(PDF_MAGIC):
        raise pdf_module.PdfError("PDF conversion produced no PDF")
    removed = getattr(result, "removed_blank_pages", None)
    toc = getattr(result, "toc_updated", None)
    return PdfOutcome(
        data,
        pages=_whole(getattr(result, "pages", None), minimum=1),  # 0: not known
        removed_blank=len(removed) if isinstance(removed, tuple | list) else None,
        toc_updated=toc if isinstance(toc, bool) else None,
        fix_count=_tidy_total(result),
    )


def render_pdf(docx: bytes, *, tidy: bool = True) -> PdfOutcome:
    """Render with the PDF renderer; its errors (``PdfUnavailable`` / ``PdfBusy`` /
    ``PdfError``) pass through for the route to map."""
    convert = getattr(pdf_module, "convert_to_pdf_report", None)
    if convert is None:  # a renderer without the report function: the plain conversion
        data = pdf_module.convert_to_pdf(docx)
        if not isinstance(data, bytes) or not data.startswith(PDF_MAGIC):
            raise pdf_module.PdfError("PDF conversion produced no PDF")
        return PdfOutcome(data)
    return _outcome(convert(docx, tidy=tidy))


# -- response headers ------------------------------------------------------------------------
def _digits(value: int) -> str:
    return str(min(max(int(value), 0), MAX_HEADER_NUMBER))


def layout_headers(fix_count: int, pdf: PdfOutcome | None = None) -> dict[str, str]:
    """``X-Layout-Fixes`` always; for a PDF also the page count, the removed blank pages and
    whether the table of contents was refreshed, when known. ASCII digits only."""
    headers = {"X-Layout-Fixes": _digits(fix_count)}
    if pdf is None:
        return headers
    if pdf.pages is not None:
        headers["X-Pdf-Pages"] = _digits(pdf.pages)
    if pdf.removed_blank is not None:
        headers["X-Pdf-Removed-Blank"] = _digits(pdf.removed_blank)
    if pdf.toc_updated is not None:
        headers["X-Pdf-Toc-Updated"] = "1" if pdf.toc_updated else "0"
    return headers
