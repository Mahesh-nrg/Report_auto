"""Routes of the Export tab: upload a DOCX or PDF, detect its sections, choose fonts, download.

Contract: docs/EXPORT_DESIGN.md (c). Prefix ``/__ai/api/docx/export``: every method (GET too)
needs the ``X-Requested-With`` header, and the prefix gets the no-store headers. Open to every
signed-in user; a file belongs to its uploader and is a plain 404 for anybody else (an
administrator included). Ids are uuid4 hex. Errors are fixed sentences (``export_errors``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from .api_deps import GatewayDep, UserDep, limited, require_csrf_header
from .export_errors import ExportFailure, ExportInputError, ExportLimitError, ExportUnavailable
from .export_store import MAX_EXPORT_PDF_BYTES
from .gateway import Gateway, Identity, docx_installed
from .layout_service import (
    PdfOutcome,
    check_payload,
    layout_headers,
    parse_tidy,
    render_pdf,
    tidy_for_download,
)
from .limits import Scope
from .pdf import (
    REFUSED_EXTERNAL,
    REFUSED_FIELD,
    REFUSED_IMAGES,
    REFUSED_INVALID,
    REFUSED_LARGE,
    PdfBusy,
    PdfError,
    PdfUnavailable,
    check_document,
)
from .pdf_import.model import engine_choice
from .reports import DOCX_TYPE, PDF_TYPE, content_disposition, report_stem
from .server_docx import DocxBodyLimit
from .upload_stream import receive_upload
from .uploads import MULTIPART_OVERHEAD, UploadError

log = logging.getLogger(__name__)

PREFIX = "/__ai/api/docx/export"
UPLOAD_PATH = f"{PREFIX}/files"
UPLOAD_SECONDS = 600.0  # a 100 MB PDF at about 170 KB/s
MAX_JSON_BODY = 256 * 1024  # a plan for 2000 sections fits easily
FORMATS = ("docx", "pdf")
REFUSALS = frozenset(
    {REFUSED_EXTERNAL, REFUSED_FIELD, REFUSED_IMAGES, REFUSED_INVALID, REFUSED_LARGE}
)
WORD_STILL_WORKS = "The Word file can still be downloaded."
EXPORT_SUFFIX = "-fonts"

router = APIRouter(prefix=PREFIX, dependencies=[Depends(require_csrf_header)])
T = TypeVar("T")


class ExportBodyLimit(DocxBodyLimit):
    """:class:`DocxBodyLimit` that also lets the export upload route carry one 100 MB PDF."""

    def limit_for(self, scope: Scope) -> int:
        if scope["method"] == "POST" and scope["path"] == UPLOAD_PATH:
            return MAX_EXPORT_PDF_BYTES + MULTIPART_OVERHEAD
        return super().limit_for(scope)


# -- helpers ----------------------------------------------------------------------------------
def _owner(gw: Gateway, identity: Identity) -> str:
    return gw.docx_owner(identity)


def _fail(exc: ExportFailure) -> HTTPException:
    return HTTPException(exc.status, str(exc))


def _call(action: Callable[[], T]) -> T:
    """Run ``action`` mapping export errors to their status and fixed sentence."""
    try:
        return action()
    except ExportFailure as exc:
        raise _fail(exc) from exc


def _installed() -> None:
    if not docx_installed():
        raise _fail(ExportUnavailable("docx"))


async def _json_body(request: Request) -> Any:
    raw = await request.body()
    if len(raw) > MAX_JSON_BODY:
        raise _fail(ExportLimitError("too_large"))
    try:
        return json.loads(raw) if raw else {}
    except (ValueError, RecursionError) as exc:
        raise _fail(ExportInputError("invalid", field="body")) from exc


# -- catalog ----------------------------------------------------------------------------------
@router.get("/fonts")
def fonts(identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    with limited(gw, "export_read", identity.username):
        try:
            return gw.export.fonts().to_json()
        except ExportFailure as exc:
            raise _fail(exc) from exc
        except Exception as exc:  # noqa: BLE001 - fontconfig trouble: no detail, no 500 text
            log.warning("font catalog failed (%s)", type(exc).__name__)
            raise HTTPException(502, "the font list could not be read") from exc


@router.get("/engines")
def engines(identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    with limited(gw, "export_read", identity.username):
        return {"engines": [info.to_json() for info in gw.export.engines()], "ocr": False}


# -- files ------------------------------------------------------------------------------------
@router.get("/files")
def list_files(identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    with limited(gw, "export_read", identity.username):
        return gw.export.listing(_owner(gw, identity))


@router.post("/files", status_code=201)
async def upload_file(
    request: Request, identity: UserDep, gw: GatewayDep, engine: str = "auto"
) -> dict[str, Any]:
    """Store ONE .docx or .pdf (multipart field ``file``); the analysis is started separately."""
    _installed()
    _call(lambda: engine_choice(engine, "engine"))
    owner = _owner(gw, identity)
    with limited(gw, "export_upload", identity.username):
        try:
            staged = await receive_upload(
                request,
                gw.export.store.uploads(owner),  # type: ignore[arg-type]
                deadline=UPLOAD_SECONDS,
            )
        except UploadError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        record = await run_in_threadpool(_call, lambda: gw.export.ingest(staged, engine))
    return gw.export.item(record)


@router.get("/files/{file_id}")
def file_detail(file_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    with limited(gw, "export_read", identity.username):
        return _call(lambda: gw.export.detail(_owner(gw, identity), file_id))


@router.get("/files/{file_id}/status")
def file_status(file_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    with limited(gw, "export_status", identity.username):
        return _call(lambda: gw.export.status(_owner(gw, identity), file_id))


@router.post("/files/{file_id}/analyse", status_code=202)
async def analyse_file(
    file_id: str, request: Request, identity: UserDep, gw: GatewayDep
) -> dict[str, bool]:
    """Start converting (PDF) and detecting the sections in the background (poll /status)."""
    _installed()
    body = await _json_body(request)
    owner = _owner(gw, identity)

    def start() -> None:
        with limited(gw, "export_run", identity.username):
            _call(lambda: gw.export.start(owner, file_id, body))

    await run_in_threadpool(start)
    return {"started": True}


@router.post("/files/{file_id}/cancel")
def cancel_file(file_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, bool]:
    with limited(gw, "export_run", identity.username):
        return _call(lambda: gw.export.cancel(_owner(gw, identity), file_id))


@router.put("/files/{file_id}/plan")
async def save_plan(
    file_id: str, request: Request, identity: UserDep, gw: GatewayDep
) -> dict[str, Any]:
    body = await _json_body(request)
    owner = _owner(gw, identity)

    def save() -> dict[str, Any]:
        with limited(gw, "export_run", identity.username):
            return _call(lambda: gw.export.save_plan(owner, file_id, body))

    return await run_in_threadpool(save)


@router.delete("/files/{file_id}")
def delete_file(file_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, bool]:
    with limited(gw, "export_upload", identity.username):
        _call(lambda: gw.export.delete(_owner(gw, identity), file_id))
    return {"deleted": True}


# -- download ---------------------------------------------------------------------------------
def _pdf(gw: Gateway, docx: bytes, *, tidy: bool) -> tuple[bytes, dict[str, str]]:
    """LibreOffice conversion of the typographed bytes (cached per bytes and tidy choice)."""
    key = gw.docx_pdf_cache.key(docx, "export|tidy" if tidy else "export")
    cached = gw.docx_pdf_cache.entry(key)
    if cached is not None:
        outcome = PdfOutcome(cached[0], **cached[1])
        return outcome.data, layout_headers(outcome.fix_count, outcome)
    try:
        check_document(docx)
        produced = render_pdf(docx, tidy=tidy)
    except PdfUnavailable as exc:
        raise _fail(ExportUnavailable("renderer")) from exc
    except PdfBusy as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": "10"}) from exc
    except PdfError as exc:
        if str(exc) in REFUSALS:
            raise HTTPException(422, f"{exc}. {WORD_STILL_WORKS}") from exc
        log.warning("export PDF conversion failed (%s)", type(exc).__name__)
        raise HTTPException(502, "PDF conversion failed; see the gateway log") from exc
    gw.docx_pdf_cache.put(key, produced.data, produced.facts())
    return produced.data, layout_headers(produced.fix_count, produced)


def _deliver(gw: Gateway, data: bytes, fmt: str, *, tidy: bool) -> tuple[bytes, dict[str, str]]:
    if fmt == "pdf":
        return _pdf(gw, data, tidy=tidy)
    tidied = tidy_for_download(data, enabled=tidy)
    return tidied.data, layout_headers(tidied.fix_count)


@router.get("/files/{file_id}/download")
def download_file(
    file_id: str,
    identity: UserDep,
    gw: GatewayDep,
    format: str = "docx",  # noqa: A002 - the query parameter is named ``format``
    tidy: str = "1",
) -> Response:
    """The analysed document with the font plan applied, as Word or PDF."""
    if format not in FORMATS:
        raise _fail(ExportInputError("invalid", field="format"))
    tidy_on = parse_tidy(tidy)
    with limited(gw, "export_download", identity.username):
        record, rendered = _call(lambda: gw.export.render(_owner(gw, identity), file_id))
        body, layout = _deliver(gw, rendered.data, format, tidy=tidy_on)
        substitutes = gw.export.substitutes(rendered.families)
    stem = report_stem(Path(record.name).stem) + EXPORT_SUFFIX
    headers = {
        "Content-Disposition": content_disposition(stem, format),
        **rendered.headers,
        **layout,
        "X-Font-Substitutes": str(min(substitutes, 999)),
    }
    media_type = PDF_TYPE if format == "pdf" else DOCX_TYPE
    return Response(body, media_type=media_type, headers=headers)


@router.get("/files/{file_id}/check")
def check_file(file_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    """Layout facts and font substitutions of the typographed DOCX, as it would be downloaded."""
    with limited(gw, "export_download", identity.username):
        _, rendered = _call(lambda: gw.export.render(_owner(gw, identity), file_id))
        return check_payload(rendered.data, tidy=True)
