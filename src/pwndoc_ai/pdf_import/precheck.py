"""Hostile-PDF precheck: bounded work with pypdf and zlib, no rendering.

Runs at upload (in process, 15 s deadline) and again inside the worker. It refuses what the
contract refuses (docs/EXPORT_DESIGN.md (g)) and returns the page count recounted from the page
tree (never ``/Count``). Stream sizes are measured with a bounded ``zlib`` decode, so a flate
bomb costs at most the per-page budget.
"""

from __future__ import annotations

import io
import time
import zlib
from pathlib import Path
from typing import Any

from ..export_errors import ExportLimitError
from . import limits
from .errors import PdfRefused
from .model import PdfFacts

ACTION_CODES = {
    "/JavaScript": "javascript",
    "/Launch": "launch",
    "/GoToR": "remote_action",
    "/GoToE": "remote_action",
    "/SubmitForm": "forms",
    "/ImportData": "forms",
    "/Rendition": "media",
    "/Movie": "media",
    "/Sound": "media",
    "/RichMedia": "media",
}
ANNOT_CODES = {
    "/FileAttachment": "embedded_files",
    "/Movie": "media",
    "/Sound": "media",
    "/Screen": "media",
    "/RichMedia": "media",
    "/3D": "media",
}


def _expire(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise PdfRefused("content_budget")


def _resolve(value: Any) -> Any:
    return value.get_object() if hasattr(value, "get_object") else value


def _is_dict(value: Any) -> bool:
    return hasattr(value, "keys") and hasattr(value, "get")


def _check_action(value: Any, depth: int = 0, seen: set[int] | None = None) -> None:
    """Follow an action, its ``/Next`` chain and ``/AA`` dictionaries up to the depth limit."""
    seen = set() if seen is None else seen
    target = _resolve(value)
    if depth > limits.MAX_ACTION_DEPTH or target is None or id(target) in seen:
        return
    seen.add(id(target))
    if isinstance(target, list):
        for item in target[:64]:
            _check_action(item, depth + 1, seen)
        return
    if not _is_dict(target):
        return
    if "/JS" in target:
        raise PdfRefused("javascript")
    code = ACTION_CODES.get(str(target.get("/S", "")))
    if code:
        raise PdfRefused(code)
    for key in ("/Next", "/A"):
        if key in target:
            _check_action(target[key], depth + 1, seen)
    _check_additional(target.get("/AA"), depth + 1, seen)


def _check_additional(value: Any, depth: int, seen: set[int]) -> None:
    target = _resolve(value)
    if _is_dict(target):
        for item in list(target.values())[:32]:
            _check_action(item, depth, seen)


def _check_catalog(root: Any) -> None:
    for key in ("/OpenAction", "/AA"):
        if key in root:
            _check_action(root[key])
    names = _resolve(root.get("/Names"))
    if _is_dict(names):
        if "/JavaScript" in names:
            raise PdfRefused("javascript")
        if "/EmbeddedFiles" in names:
            raise PdfRefused("embedded_files")
    if "/AF" in root:
        raise PdfRefused("embedded_files")
    form = _resolve(root.get("/AcroForm"))
    if _is_dict(form) and "/XFA" in form:
        raise PdfRefused("forms")


def _check_annotations(page: Any) -> None:
    annots = _resolve(page.get("/Annots"))
    if not isinstance(annots, list):
        return
    for item in annots[: limits.MAX_ANNOTS_PER_PAGE]:
        annot = _resolve(item)
        if not _is_dict(annot):
            continue
        code = ANNOT_CODES.get(str(annot.get("/Subtype", "")))
        if code:
            raise PdfRefused(code)
        if "/AF" in annot:
            raise PdfRefused("embedded_files")
        _check_action(annot.get("/A"))
        _check_additional(annot.get("/AA"), 1, set())


_FLATE_NAMES = ("/FlateDecode", "/Fl")
_MAX_FILTER_CHAIN = 3


def _inflate(data: bytes, cap: int) -> bytes | None:
    """One bounded Flate stage: at most ``cap + 1`` bytes, ``None`` when the data is corrupt."""
    try:
        return zlib.decompressobj().decompress(data, cap + 1)
    except zlib.error:
        return None


def bounded_size(stream: Any, cap: int) -> int:
    """Decoded size of one stream, stopping at ``cap + 1`` bytes. Every leading Flate stage of
    the filter chain is decoded in turn; a chain longer than ``_MAX_FILTER_CHAIN`` or with any
    filter after a Flate stage that is not Flate counts as over the cap (refused)."""
    raw = getattr(stream, "_data", b"") or b""
    filters = _resolve(stream.get("/Filter"))
    names = [str(item) for item in filters] if isinstance(filters, list) else [str(filters)]
    if not names or names[0] not in _FLATE_NAMES:
        return len(raw)
    if len(names) > _MAX_FILTER_CHAIN or any(n not in _FLATE_NAMES for n in names):
        return cap + 1
    data = raw
    for _ in names:
        decoded = _inflate(data, cap)
        if decoded is None:
            return len(data)
        if len(decoded) > cap:
            return cap + 1
        data = decoded
    return len(data)


class _Budget:
    """Decoded-content and image-pixel budgets of one document."""

    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.content = limits.MAX_TOTAL_STREAM
        self.pixels = limits.MAX_TOTAL_PIXELS
        self.seen: set[Any] = set()

    def contents(self, holder: Any) -> int:
        resolved = _resolve(holder)
        parts = resolved if isinstance(resolved, list) else [resolved]
        size = 0
        for part in parts[: limits.MAX_STREAM_PARTS]:
            stream = _resolve(part)
            if _is_dict(stream):
                size += bounded_size(stream, limits.MAX_PAGE_STREAM)
        return size

    def image(self, target: Any) -> None:
        try:
            pixels = int(target.get("/Width", 0)) * int(target.get("/Height", 0))
        except (TypeError, ValueError):
            raise PdfRefused("unreadable") from None
        self.pixels -= max(0, pixels)
        if pixels > limits.MAX_IMAGE_PIXELS or self.pixels < 0:
            raise PdfRefused("image_budget")

    def resources(self, resources: Any, depth: int) -> int:
        """Images measured; form XObjects counted (content) and walked to ``MAX_FORM_DEPTH``."""
        xobjects = _resolve(_resolve(resources).get("/XObject")) if _is_dict(resources) else None
        if not _is_dict(xobjects):
            return 0
        size = 0
        for reference in list(xobjects.values())[: limits.MAX_XOBJECTS_PER_RESOURCE]:
            _expire(self.deadline)
            key = getattr(reference, "idnum", None) or id(reference)
            target = _resolve(reference)
            if key in self.seen or not _is_dict(target):
                continue
            self.seen.add(key)
            subtype = str(target.get("/Subtype", ""))
            if subtype == "/Image":
                self.image(target)
            elif subtype == "/Form" and depth < limits.MAX_FORM_DEPTH:
                size += bounded_size(target, limits.MAX_PAGE_STREAM)
                size += self.resources(_resolve(target.get("/Resources")), depth + 1)
        return size

    def page(self, page: Any) -> None:
        self.seen = set()
        size = self.contents(page.get("/Contents")) if "/Contents" in page else 0
        size += self.resources(_resolve(page.get("/Resources")), 0)
        self.content -= size
        if size > limits.MAX_PAGE_STREAM or self.content < 0:
            raise PdfRefused("content_budget")


def _check_box(page: Any) -> None:
    try:
        box = page.mediabox
        width, height = float(box.width), float(box.height)
    except Exception:  # noqa: BLE001 - a damaged box is a damaged file
        raise PdfRefused("unreadable") from None
    if not (0 < abs(width) <= limits.MAX_PAGE_SIDE and 0 < abs(height) <= limits.MAX_PAGE_SIDE):
        raise PdfRefused("page_too_big")


def _read_head(path: Path) -> bytes:
    try:
        size = path.stat().st_size
        if size > limits.MAX_PDF_BYTES:
            raise ExportLimitError("too_large")
        data = path.read_bytes()
    except OSError:
        raise PdfRefused("unreadable") from None
    if b"%PDF-" not in data[: limits.HEAD_BYTES]:
        raise PdfRefused("not_pdf")
    return data


def _open(data: bytes) -> tuple[Any, int]:
    try:
        import pypdf
    except ImportError:
        from .errors import PdfEngineUnavailable

        raise PdfEngineUnavailable("pdf_tools") from None
    try:
        reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise PdfRefused("encrypted")
        count = len(reader.pages)
    except PdfRefused:
        raise
    except Exception:  # noqa: BLE001 - pypdf raises many types (and RecursionError) on damage
        raise PdfRefused("unreadable") from None
    return reader, count


def _walk_pages(reader: Any, deadline: float) -> None:
    budget = _Budget(deadline)
    for page in reader.pages:
        _expire(deadline)
        _check_box(page)
        _check_annotations(page)
        if "/AA" in page:
            _check_additional(page["/AA"], 1, set())
        if "/AF" in page:
            raise PdfRefused("embedded_files")
        budget.page(page)


def precheck_pdf(path: Path, *, deadline: float) -> PdfFacts:
    """Facts about a PDF that passed every check; ``PdfRefused`` or ``ExportLimitError`` else."""
    data = _read_head(path)
    reader, count = _open(data)
    if count < 1:
        raise PdfRefused("no_pages")
    if count > limits.MAX_PAGES:
        raise ExportLimitError("too_many_pages")
    try:
        root = _resolve(reader.trailer["/Root"])
        if not _is_dict(root):
            raise PdfRefused("unreadable")
        _check_catalog(root)
        _walk_pages(reader, deadline)
        tagged = "/StructTreeRoot" in root
    except (PdfRefused, ExportLimitError):
        raise
    except Exception:  # noqa: BLE001 - damaged objects surface as many pypdf types
        raise PdfRefused("unreadable") from None
    return PdfFacts(pages=count, size=len(data), tagged=tagged)


def quick_precheck(path: Path) -> PdfFacts:
    """The upload check: :func:`precheck_pdf` with the 15 s deadline."""
    return precheck_pdf(path, deadline=time.monotonic() + limits.PRECHECK_SECONDS)


__all__ = ["bounded_size", "precheck_pdf", "quick_precheck"]
