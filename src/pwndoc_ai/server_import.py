"""Routes of the DOCX importer: analyse an uploaded report, edit the preview, create it in PwnDoc,
undo it. Contract: docs/IMPORTER_DESIGN.md §c.

The file itself is uploaded through ``POST /__ai/api/docx/files`` (docx_store precheck, quota and
owner isolation); these routes only ever name its ``file_id``. Every route needs the
``X-Requested-With`` header (GET included), a signed-in PwnDoc user and a per-user throttle. An
import belongs to the user who started it: for anybody else, an administrator included, it does
not exist (404). PwnDoc is called with the user's own, newest session cookie. Errors are fixed
sentences (``docx_import.errors``), never a path, a document text or an upstream message.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from .api_deps import GatewayDep, UserDep, limited, pwndoc_http_error, require_csrf_header
from .docx_import import limits, verify
from .docx_import.errors import ImportFailure, ImportInputError, ImportNotFound
from .docx_import.jobs import OpenClient
from .docx_import.model import AnalyseRequest, CreateRequest, ImportPlan, PlanEdits
from .docx_jobs import EngineUnavailable
from .gateway import Gateway, Identity
from .pwndoc_client import PwnDocError

PREFIX = "/__ai/api/docx/import"
IMAGE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'",
    "Cache-Control": "no-store",
}

router = APIRouter(prefix=PREFIX, dependencies=[Depends(require_csrf_header)])


# -- helpers ----------------------------------------------------------------------------------
@contextmanager
def _answers() -> Iterator[None]:
    """Turn the importer's errors into their HTTP answers (fixed sentences only)."""
    try:
        yield
    except ImportFailure as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except PwnDocError as exc:
        raise pwndoc_http_error(exc) from exc
    except EngineUnavailable as exc:
        raise HTTPException(501, str(exc)) from exc


@contextmanager
def _call(gw: Gateway, throttle: str, identity: Identity) -> Iterator[None]:
    with limited(gw, throttle, identity.username), _answers():
        yield


def _owner(gw: Gateway, identity: Identity) -> str:
    return gw.docx_owner(identity)


def _open_client(gw: Gateway, identity: Identity) -> OpenClient:
    """A fresh PwnDoc client per phase, with the newest token PwnDoc gave this user."""
    return lambda: gw.client(gw.session_cookies(identity.username) or identity.cookies)


def _import_id(value: str) -> str:
    if not limits.IMPORT_ID.fullmatch(value or ""):
        raise ImportNotFound()
    return value


def _json(raw: bytes, *, empty: Any = None) -> Any:
    """The parsed body; an empty body is ``empty`` when allowed. Anything else is a 400."""
    if not raw.strip() and empty is not None:
        return empty
    try:
        return json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ImportInputError() from exc


def _empty_body(raw: bytes) -> None:
    if _json(raw, empty={}) != {}:
        raise ImportInputError()


# -- listing and starting ---------------------------------------------------------------------
@router.get("/imports")
def list_imports(identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    with _call(gw, "import_read", identity):
        statuses = gw.docx_import.list(_owner(gw, identity))
    return {"imports": [s.to_json() for s in statuses], "limit": limits.MAX_IMPORTS_PER_OWNER}


def _start(gw: Gateway, identity: Identity, raw: bytes) -> dict[str, Any]:
    with _call(gw, "import_run", identity):
        req = AnalyseRequest.from_json(_json(raw))
        status = gw.docx_import.start_analysis(
            identity, _owner(gw, identity), req, _open_client(gw, identity)
        )
    return status.to_json()


@router.post("/imports", status_code=202)
async def start_import(request: Request, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    """Analyse an uploaded DOCX (``{"file_id", "use_llm"?}``) in the background."""
    raw = await request.body()
    return await run_in_threadpool(_start, gw, identity, raw)


@router.get("/imports/{import_id}")
def import_status(import_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    with _call(gw, "import_status", identity):
        status = gw.docx_import.get_status(_owner(gw, identity), _import_id(import_id))
    return status.to_json()


def _with_verification(plan: ImportPlan) -> dict[str, Any]:
    """The plan (findings already in docs/FINDING_ORDER.md order) plus its order/severity
    verification report; the client never sends the order back, it is recomputed per edit."""
    return {**plan.to_json(), "verification": verify.verify_plan(plan)}


def _verification(gw: Gateway, identity: Identity, import_id: str) -> dict[str, Any]:
    owner = _owner(gw, identity)
    status = gw.docx_import.get_status(owner, _import_id(import_id))
    result = status.result
    if result is None or status.undo is not None:
        plan = gw.docx_import.preview(owner, import_id)
        return {"source": "plan", **verify.verify_plan(plan)}
    audit = _open_client(gw, identity)().get_audit(result.audit_id)
    findings = audit.get("findings") if isinstance(audit, dict) else None
    return {
        "source": "audit",
        **verify.verify_audit(findings if isinstance(findings, list) else []),
    }


@router.get("/imports/{import_id}/verification")
def get_verification(import_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    """The verification of the created audit (PwnDoc's stored order), else of the preview."""
    with _call(gw, "import_read", identity):
        return _verification(gw, identity, import_id)


# -- the preview ------------------------------------------------------------------------------
@router.get("/imports/{import_id}/preview")
def get_preview(import_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    with _call(gw, "import_read", identity):
        plan = gw.docx_import.preview(_owner(gw, identity), _import_id(import_id))
    return _with_verification(plan)


def _edit(gw: Gateway, identity: Identity, import_id: str, raw: bytes) -> dict[str, Any]:
    with _call(gw, "import_edit", identity):
        owner = _owner(gw, identity)
        gw.docx_import.get_status(owner, _import_id(import_id))  # 404 before any body error
        edits = PlanEdits.from_json(_json(raw))
        plan = gw.docx_import.edit(owner, import_id, edits, _open_client(gw, identity))
    return _with_verification(plan)


@router.put("/imports/{import_id}/preview")
async def edit_preview(
    import_id: str, request: Request, identity: UserDep, gw: GatewayDep
) -> dict[str, Any]:
    """Apply a partial edit (with the plan revision) and answer with the whole plan."""
    raw = await request.body()
    return await run_in_threadpool(_edit, gw, identity, import_id, raw)


@router.get("/imports/{import_id}/images/{key}")
def get_image(import_id: str, key: str, identity: UserDep, gw: GatewayDep) -> Response:
    with _call(gw, "import_read", identity):
        if not limits.IMAGE_KEY.fullmatch(key or ""):
            raise ImportNotFound("image")
        data, media_type = gw.docx_import.image(_owner(gw, identity), _import_id(import_id), key)
    return Response(data, media_type=media_type, headers=IMAGE_HEADERS)


# -- create, cancel, undo, delete -------------------------------------------------------------
def _create(gw: Gateway, identity: Identity, import_id: str, raw: bytes) -> dict[str, Any]:
    with _call(gw, "import_write", identity):
        owner = _owner(gw, identity)
        gw.docx_import.get_status(owner, _import_id(import_id))
        req = CreateRequest.from_json(_json(raw))
        status = gw.docx_import.start_create(
            identity, owner, import_id, req, _open_client(gw, identity)
        )
    return status.to_json()


@router.post("/imports/{import_id}/create", status_code=202)
async def create_import(
    import_id: str, request: Request, identity: UserDep, gw: GatewayDep
) -> dict[str, Any]:
    """Create the company, audit, findings and screenshots in PwnDoc (background step)."""
    raw = await request.body()
    return await run_in_threadpool(_create, gw, identity, import_id, raw)


def _cancel(gw: Gateway, identity: Identity, import_id: str, raw: bytes) -> dict[str, Any]:
    with _call(gw, "import_run", identity):
        owner = _owner(gw, identity)
        gw.docx_import.get_status(owner, _import_id(import_id))
        _empty_body(raw)
        status = gw.docx_import.cancel(owner, import_id)
    return status.to_json()


@router.post("/imports/{import_id}/cancel")
async def cancel_import(
    import_id: str, request: Request, identity: UserDep, gw: GatewayDep
) -> dict[str, Any]:
    raw = await request.body()
    return await run_in_threadpool(_cancel, gw, identity, import_id, raw)


def _undo(gw: Gateway, identity: Identity, import_id: str, raw: bytes) -> dict[str, Any]:
    with _call(gw, "import_write", identity):
        owner = _owner(gw, identity)
        gw.docx_import.get_status(owner, _import_id(import_id))
        _empty_body(raw)
        status = gw.docx_import.start_undo(identity, owner, import_id, _open_client(gw, identity))
    return status.to_json()


@router.post("/imports/{import_id}/undo", status_code=202)
async def undo_import(
    import_id: str, request: Request, identity: UserDep, gw: GatewayDep
) -> dict[str, Any]:
    """Delete exactly what this import created in PwnDoc (background step)."""
    raw = await request.body()
    return await run_in_threadpool(_undo, gw, identity, import_id, raw)


@router.delete("/imports/{import_id}", status_code=204)
def delete_import(import_id: str, identity: UserDep, gw: GatewayDep) -> Response:
    """Remove the local preview, screenshots and journal; PwnDoc is never touched."""
    with _call(gw, "import_run", identity):
        gw.docx_import.delete(_owner(gw, identity), _import_id(import_id))
    return Response(status_code=204)
