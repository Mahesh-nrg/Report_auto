"""Routes of the DOCX refine feature: upload an existing Word report, refine it, download it.

Open to every signed-in user (not admin-only), every method (GET included) needs the
``X-Requested-With`` header, and a file belongs to the user who uploaded it: for anybody else (an
administrator included) it simply does not exist (404). Ids are uuid4 hex; errors are short fixed
sentences, never a path, a document text or a provider message.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from .api_deps import GatewayDep, UserDep, limited, require_csrf_header
from .docx_jobs import (
    ConflictError,
    DocxJobError,
    EngineUnavailable,
    clean_text,
)
from .docx_store import (
    DAMAGED,
    DAMAGED_NAME,
    MAX_DOCX_BYTES,
    DocxNotFound,
    DocxRecord,
    StagedUpload,
)
from .gateway import Gateway, Identity
from .layout_service import (
    PdfOutcome,
    check_payload,
    layout_headers,
    layout_limited,
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
from .reports import DOCX_TYPE, PDF_TYPE, content_disposition, report_stem
from .typography_service import TYPOGRAPHY_OFF, apply_defaults
from .upload_stream import UploadBodyLimit, receive_upload
from .uploads import MULTIPART_OVERHEAD, UploadError

log = logging.getLogger(__name__)

UPLOAD_PATH = "/__ai/api/docx/files"
STAT_KEYS = ("paragraphs", "refinable", "protected", "tables", "images", "sections", "words")
UPLOAD_SECONDS = 300.0  # the whole transfer of one report (50 MB at about 170 KB/s)
MAX_OUTLINE = 2000
MAX_TITLE = 200
MAX_ERROR = 200
DEFAULT_PAGE, MAX_PAGE = 50, 100
NUMBER = re.compile(r"\d{1,9}")
REFINE_KEYS = frozenset({"mode", "section_ids", "client_terms"})
KINDS = ("original", "clean", "tracked")
FORMATS = ("docx", "pdf")
SUFFIXES = {"original": "", "clean": "-refined", "tracked": "-tracked-changes"}
REFUSALS = frozenset(
    {REFUSED_EXTERNAL, REFUSED_FIELD, REFUSED_IMAGES, REFUSED_INVALID, REFUSED_LARGE}
)
NOT_FOUND = "file not found"
UNREADABLE = "the document could not be read"
BAD_BODY = 'send a JSON body like {"mode": "light", "section_ids": null}'
NOT_REFINED = "this document has not been refined yet: run Refine first"
RUNNING = "a refinement is running on this file; cancel it first"
PDF_WORD_STILL_WORKS = "The Word file can still be downloaded."

router = APIRouter(prefix="/__ai/api/docx", dependencies=[Depends(require_csrf_header)])


class DocxBodyLimit(UploadBodyLimit):
    """:class:`UploadBodyLimit` that also lets the DOCX upload route carry one 50 MB report."""

    def limit_for(self, scope: Scope) -> int:
        if scope["method"] == "POST" and scope["path"] == UPLOAD_PATH:
            return MAX_DOCX_BYTES + MULTIPART_OVERHEAD
        return super().limit_for(scope)


# -- helpers ----------------------------------------------------------------------------------
def _owner(gw: Gateway, identity: Identity) -> str:
    """The key a user's documents are stored under: the PwnDoc account id when PwnDoc sends one
    (a user name can be freed and given to somebody else), else the user name."""
    return gw.docx_owner(identity)


def _record(gw: Gateway, owner: str, file_id: str) -> DocxRecord:
    try:
        return gw.docx_store.get(owner, file_id)
    except DocxNotFound as exc:
        raise HTTPException(404, NOT_FOUND) from exc


def _engine(gw: Gateway) -> Any:
    try:
        return gw.docx_engine()
    except EngineUnavailable as exc:
        raise HTTPException(501, str(exc)) from exc


def _page_number(raw: str, name: str, *, minimum: int) -> int:
    if not NUMBER.fullmatch(raw) or int(raw) < minimum:
        raise HTTPException(400, f"{name} must be a whole number of at least {minimum}")
    return int(raw)


# -- upload -----------------------------------------------------------------------------------
def _inspect(engine: Any, staged: StagedUpload) -> Any:
    docx_error = getattr(engine, "DocxError", ValueError)
    try:
        return engine.inspect_docx(staged.read_bytes())
    except docx_error as exc:
        raise HTTPException(400, clean_text(exc, MAX_ERROR) or UNREADABLE) from exc
    except Exception as exc:  # noqa: BLE001 - the document is untrusted: never a 500 or a detail
        log.warning("docx inspection failed (%s)", type(exc).__name__)
        raise HTTPException(400, UNREADABLE) from exc


def _outline(inspection: Any) -> list[dict[str, Any]]:
    return [
        {
            "id": str(s.id),
            "title": clean_text(s.title, MAX_TITLE),
            "level": int(s.level),
            "units": int(s.units),
            "chars": int(s.chars),
        }
        for s in list(inspection.sections)[:MAX_OUTLINE]
    ]


def _ingest(gw: Gateway, engine: Any, staged: StagedUpload) -> DocxRecord:
    """Inspect the staged file with the engine, then store it (quota checked under the lock)."""
    inspection = _inspect(engine, staged)
    try:
        stats = {key: max(0, int(inspection.stats.get(key, 0))) for key in STAT_KEYS}
        outline = _outline(inspection)
    except (AttributeError, TypeError, ValueError) as exc:
        log.warning("docx inspection was malformed (%s)", type(exc).__name__)
        raise HTTPException(400, UNREADABLE) from exc
    try:
        return gw.docx_store.commit(staged, stats=stats, outline=outline)
    except UploadError as exc:
        raise HTTPException(exc.status, exc.message) from exc


@router.post("/files", status_code=201)
async def upload_file(request: Request, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    """Store ONE .docx (multipart field ``file``) and answer with its outline."""
    engine = await run_in_threadpool(_engine, gw)
    with limited(gw, "docx_upload", identity.username):
        try:
            staged = await receive_upload(
                request,
                gw.docx_store.uploads(_owner(gw, identity)),  # type: ignore[arg-type]
                deadline=UPLOAD_SECONDS,
            )
        except UploadError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        try:
            record = await run_in_threadpool(_ingest, gw, engine, staged)
        finally:
            staged.discard()
    return {
        "id": record.id,
        "name": record.name,
        "size": record.size,
        "created": record.created,
        "outline": record.outline,
        "stats": record.stats,
    }


# -- reading ----------------------------------------------------------------------------------
def _damaged_item(entry: Mapping[str, Any]) -> dict[str, Any]:
    """A list item for a directory whose metadata is unreadable: only deleting it makes sense."""
    return {
        "id": entry["id"], "name": DAMAGED_NAME, "size": int(entry["size"]), "created": "",
        "state": DAMAGED, "mode": "", "changed": 0, "rejected": 0, "protected": 0,
    }  # fmt: skip


@router.get("/files")
def list_files(identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    with limited(gw, "docx_read", identity.username):
        records = gw.docx_store.list(_owner(gw, identity))
        damaged = gw.docx_store.damaged(_owner(gw, identity))
        return {
            "files": [r.item() for r in records] + [_damaged_item(d) for d in damaged],
            "quota": gw.docx_store.quota(_owner(gw, identity)),
        }


@router.get("/files/{file_id}")
def file_detail(file_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    with limited(gw, "docx_read", identity.username):
        return _record(gw, _owner(gw, identity), file_id).detail()


@router.get("/files/{file_id}/status")
def file_status(file_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    with limited(gw, "docx_status", identity.username):
        _record(gw, _owner(gw, identity), file_id)
        return _status(gw, _owner(gw, identity), file_id)


def _status(gw: Gateway, owner: str, file_id: str) -> dict[str, Any]:
    try:
        return gw.docx_jobs.status(owner, file_id)
    except DocxNotFound as exc:
        raise HTTPException(404, NOT_FOUND) from exc


def _with_section_titles(
    rows: list[dict[str, Any]], outline: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Copies of the change rows whose ``section`` is the heading title (the engine stores ids)."""
    titles = {str(o.get("id")): str(o.get("title") or "") for o in outline}
    return [
        {**row, "section": titles.get(str(row.get("section")), "") or row.get("section", "")}
        for row in rows
    ]


@router.get("/files/{file_id}/changes")
def file_changes(
    file_id: str,
    identity: UserDep,
    gw: GatewayDep,
    offset: str = "0",
    limit: str = str(DEFAULT_PAGE),
) -> dict[str, Any]:
    """One page of the change list of the last finished run (an empty list before any run)."""
    with limited(gw, "docx_read", identity.username):
        record = _record(gw, _owner(gw, identity), file_id)
        start = _page_number(offset, "offset", minimum=0)
        size = min(_page_number(limit, "limit", minimum=1), MAX_PAGE)
        try:
            changes = gw.docx_store.load_changes(_owner(gw, identity), file_id)
        except DocxNotFound as exc:
            raise HTTPException(404, NOT_FOUND) from exc
        page = _with_section_titles(changes[start : start + size], record.outline)
        return {"total": len(changes), "changes": page}


# -- refine, cancel, delete -------------------------------------------------------------------
def _parse_refine_body(raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise HTTPException(400, BAD_BODY) from exc
    if not isinstance(data, dict) or "mode" not in data or not set(data) <= REFINE_KEYS:
        raise HTTPException(400, BAD_BODY)
    return data


def _start_run(gw: Gateway, owner: str, file_id: str, raw: bytes) -> dict[str, bool]:
    with limited(gw, "docx_run", owner):
        _record(gw, owner, file_id)  # 404 first: a body error must not hint at other files
        body = _parse_refine_body(raw)
        try:
            gw.docx_jobs.start(
                owner,
                file_id,
                mode=body["mode"],
                section_ids=body.get("section_ids"),
                client_terms=body.get("client_terms", []),
            )
        except DocxNotFound as exc:
            raise HTTPException(404, NOT_FOUND) from exc
        except DocxJobError as exc:  # includes NoLlmError; fixed sentences
            raise HTTPException(400, str(exc)) from exc
        except EngineUnavailable as exc:
            raise HTTPException(501, str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
    return {"started": True}


@router.post("/files/{file_id}/refine")
async def refine_file(
    file_id: str, request: Request, identity: UserDep, gw: GatewayDep
) -> dict[str, bool]:
    """Start refining the document in the background (poll ``/status``)."""
    raw = await request.body()
    return await run_in_threadpool(_start_run, gw, _owner(gw, identity), file_id, raw)


@router.post("/files/{file_id}/cancel")
def cancel_run(file_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    with limited(gw, "docx_run", identity.username):
        _record(gw, _owner(gw, identity), file_id)
        try:
            return gw.docx_jobs.cancel(_owner(gw, identity), file_id)
        except DocxNotFound as exc:
            raise HTTPException(404, NOT_FOUND) from exc


@router.delete("/files/{file_id}")
def delete_file(file_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, bool]:
    with limited(gw, "docx_run", identity.username):
        # No record lookup: a document whose metadata is unreadable must stay deletable. The
        # store only ever finds directories under the caller's own hash (404 for anybody else's).
        if gw.docx_jobs.is_active(_owner(gw, identity), file_id):
            raise HTTPException(409, RUNNING)
        try:
            gw.docx_store.delete(_owner(gw, identity), file_id)
        except DocxNotFound as exc:
            raise HTTPException(404, NOT_FOUND) from exc
    return {"deleted": True}


# -- download ---------------------------------------------------------------------------------
def _to_pdf(gw: Gateway, docx: bytes, *, tidy: bool) -> PdfOutcome:
    """LibreOffice conversion (kept, with what the renderer reported, for the next download of
    the same bytes and tidy choice); a document the converter refuses is a 422 (Word still works).
    """
    key = gw.docx_pdf_cache.key(docx, "tidy" if tidy else "")
    cached = gw.docx_pdf_cache.entry(key)
    if cached is not None:
        return PdfOutcome(cached[0], **cached[1])
    try:
        check_document(docx)
        produced = render_pdf(docx, tidy=tidy)
        gw.docx_pdf_cache.put(key, produced.data, produced.facts())
        return produced
    except PdfUnavailable as exc:
        raise HTTPException(501, str(exc)) from exc
    except PdfBusy as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": "10"}) from exc
    except PdfError as exc:
        if str(exc) in REFUSALS:  # fixed texts of pdf.check_document
            raise HTTPException(422, f"{exc}. {PDF_WORD_STILL_WORKS}") from exc
        log.warning("docx PDF conversion failed (%s)", type(exc).__name__)
        raise HTTPException(502, "PDF conversion failed; see the gateway log") from exc


def _bytes_of(gw: Gateway, owner: str, record: DocxRecord, kind: str) -> bytes:
    try:
        return gw.docx_store.read_file(owner, record.id, kind)
    except DocxNotFound as exc:
        if kind == "original":
            raise HTTPException(404, NOT_FOUND) from exc
        raise HTTPException(409, NOT_REFINED) from exc


def _deliver(gw: Gateway, data: bytes, fmt: str, *, tidy: bool) -> tuple[bytes, dict[str, str]]:
    """The bytes to send and the layout headers: Word is tidied here, a PDF by the renderer."""
    if fmt == "pdf":
        outcome = _to_pdf(gw, data, tidy=tidy)
        return outcome.data, layout_headers(outcome.fix_count, outcome)
    tidied = tidy_for_download(data, enabled=tidy)
    return tidied.data, layout_headers(tidied.fix_count)


def _typography(gw: Gateway, data: bytes, kind: str) -> tuple[bytes, dict[str, str]]:
    """The admin's default fonts, on the clean copy only: never on the user's original file,
    never on the tracked copy (its revision markup must stay as the engine wrote it)."""
    if kind != "clean":
        return data, dict(TYPOGRAPHY_OFF)
    return apply_defaults(data, gw.store.settings)


@router.get("/files/{file_id}/download")
def download_file(
    file_id: str,
    identity: UserDep,
    gw: GatewayDep,
    kind: str = "clean",
    format: str = "docx",  # noqa: A002 - the query parameter is named ``format``
    tidy: str = "1",
) -> Response:
    """The original, the clean refined copy or the tracked-changes copy, as Word (or PDF).

    The clean copy is tidied on the way out (``tidy=0`` opts out); the original and the tracked
    copy never are, and the stored files are never touched.
    """
    if kind not in KINDS or format not in FORMATS or (format == "pdf" and kind == "tracked"):
        raise HTTPException(400, "kind must be original, clean or tracked (PDF: not tracked)")
    tidy_on = parse_tidy(tidy) and kind == "clean"
    with limited(gw, "docx_download", identity.username):
        record = _record(gw, _owner(gw, identity), file_id)
        data = _bytes_of(gw, _owner(gw, identity), record, kind)
        data, typography = _typography(gw, data, kind)
        body, layout = _deliver(gw, data, format, tidy=tidy_on)
    stem = report_stem(Path(record.name).stem) + SUFFIXES[kind]
    media_type = PDF_TYPE if format == "pdf" else DOCX_TYPE
    headers = {
        "Content-Disposition": content_disposition(stem, format),
        **typography,
        **layout,
    }
    return Response(body, media_type=media_type, headers=headers)


@router.get("/files/{file_id}/check")
def check_file(
    file_id: str, identity: UserDep, gw: GatewayDep, kind: str = "clean"
) -> dict[str, Any]:
    """Layout facts of the file as it would be downloaded, and what the tidy would change.

    Only the clean copy is ever tidied: for the original and the tracked copy the answer is the
    findings alone.
    """
    if kind not in KINDS:
        raise HTTPException(400, "kind must be original, clean or tracked")
    with layout_limited(gw, identity.username):
        record = _record(gw, _owner(gw, identity), file_id)
        data = _bytes_of(gw, _owner(gw, identity), record, kind)
        data, _ = _typography(gw, data, kind)
        return check_payload(data, tidy=kind == "clean")
