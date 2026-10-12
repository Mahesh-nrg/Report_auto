"""FastAPI gateway: reverse-proxies PwnDoc, serves the injected UI assets and the AI API."""

from __future__ import annotations

import importlib.util
import ipaddress
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

import uvicorn
from fastapi import (
    APIRouter,
    Body,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import DEFAULT_DATA_DIR, ConfigStore, SettingsError
from .gateway import Gateway, Identity, RefinerFactory, UpstreamError, raw_cookie  # noqa: F401 - re-exported
from .jobs import SAFE_ID, JobConflict, JobError, JobNotFound
from .limits import ApiHeadersMiddleware, BodyLimitMiddleware
from .llm import LlmClient, LlmError
from .prompts import ALL_FIELDS
from .proxy import proxy_http, proxy_websocket
from .pwndoc_client import PwnDocError
from .rag import SUPPORTED, safe_filename
from .tls import ensure_self_signed

log = logging.getLogger(__name__)

CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "pwndoc-ai"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
STATIC_DIR = Path(__file__).parent / "static"
# Changing these from the UI would let any PwnDoc admin redirect every user's session cookie.
LOCKED_SETTINGS = ("pwndoc_url", "verify_tls")
STATIC_TYPES = {"inject.js": "text/javascript", "inject.css": "text/css"}
MAX_FINDING_IDS = 500
LOOPBACK_NAMES = frozenset({"localhost"})
ALLOW_HTTP_ENV = "PWNDOC_AI_ALLOW_HTTP"


class InsecureBindError(RuntimeError):
    """Refusing to serve plain HTTP on a non-loopback address without explicit consent."""


# -- dependencies ----------------------------------------------------------------------------
def gateway(request: Request) -> Gateway:
    return request.app.state.gateway


GatewayDep = Annotated[Gateway, Depends(gateway)]


def csrf_guard(request: Request) -> None:
    if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(403, f"missing {CSRF_HEADER}: {CSRF_VALUE} header")


def current_user(request: Request, gw: GatewayDep) -> Identity:
    identity = gw.identity(request)
    if identity is None:
        raise HTTPException(401, "sign in to PwnDoc first")
    return identity


UserDep = Annotated[Identity, Depends(current_user)]


def admin_user(identity: UserDep) -> Identity:
    if not identity.is_admin:
        raise HTTPException(403, "administrator role required")
    return identity


AdminDep = Annotated[Identity, Depends(admin_user)]


def pwndoc_http_error(exc: PwnDocError) -> HTTPException:
    if exc.status in (401, 403, 404):
        return HTTPException(exc.status, str(exc))
    log.warning("PwnDoc call failed: %s", exc)
    return HTTPException(502, "PwnDoc request failed; see the gateway log")


# -- request models --------------------------------------------------------------------------
class JobRequest(BaseModel):
    audit_id: str = Field(pattern=SAFE_ID.pattern)
    finding_ids: list[str] | None = Field(default=None, max_length=MAX_FINDING_IDS)
    fields: list[str] | None = None
    mode: Literal["improve", "fill_missing"] = "improve"
    auto_apply: bool = False


class ApplyRequest(BaseModel):
    finding_ids: list[str] = Field(max_length=MAX_FINDING_IDS)


def _severity(finding: dict[str, Any]) -> str:
    for entry in finding.get("customFields") or []:
        field = entry.get("customField") if isinstance(entry, dict) else None
        label = (
            str(field.get("label") or field.get("display") or "") if isinstance(field, dict) else ""
        )
        if "severity" in label.lower() and isinstance(entry.get("text"), str):
            return entry["text"]
    return ""


# -- routes ----------------------------------------------------------------------------------
api = APIRouter(prefix="/__ai/api", dependencies=[Depends(csrf_guard)])


@api.get("/status")
def status(request: Request, gw: GatewayDep) -> dict[str, Any]:
    identity = gw.identity(request)
    return {
        "authenticated": identity is not None,
        "username": identity.username if identity else "",
        "role": identity.role if identity else "",
        "roles": list(identity.roles) if identity else [],
        "is_admin": bool(identity and identity.is_admin),
        "llm_configured": gw.store.settings.llm_configured,
        "rag_available": importlib.util.find_spec("chromadb") is not None,
        "version": __version__,
    }


@api.get("/settings")
def get_settings(_: AdminDep, gw: GatewayDep) -> dict[str, Any]:
    return gw.store.settings.public()


@api.put("/settings")
def put_settings(
    patch: Annotated[dict[str, Any], Body()], _: AdminDep, gw: GatewayDep
) -> dict[str, Any]:
    current = gw.store.settings
    for name in LOCKED_SETTINGS:
        if name in patch and patch[name] != getattr(current, name):
            raise HTTPException(
                400,
                f"{name} can only be changed with the CLI (`pwndoc-ai config --set`) or the "
                "environment, not from the browser",
            )
    try:
        return gw.store.update(
            {k: v for k, v in patch.items() if k not in LOCKED_SETTINGS}
        ).public()
    except SettingsError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        log.exception("could not save settings")
        raise HTTPException(
            500, "could not save settings; check that the gateway's data directory is writable"
        ) from exc


@api.post("/settings/test")
def test_llm(_: AdminDep, gw: GatewayDep) -> dict[str, Any]:
    try:
        return {"ok": True, "reply": LlmClient(gw.store.settings).ping()}
    except LlmError as exc:
        return {"ok": False, "error": str(exc)}


@api.get("/models")
def list_models(_: AdminDep, gw: GatewayDep) -> dict[str, Any]:
    try:
        return {"models": LlmClient(gw.store.settings).list_models()}
    except LlmError as exc:
        raise HTTPException(400, str(exc)) from exc


@api.get("/rag/documents")
def rag_documents(_: AdminDep, gw: GatewayDep) -> dict[str, Any]:
    index = gw.rag_index()
    return {"available": index is not None, "documents": index.list_documents() if index else []}


@api.post("/rag/documents")
def rag_upload(file: Annotated[UploadFile, File()], _: AdminDep, gw: GatewayDep) -> dict[str, Any]:
    index = gw.rag_index()
    if index is None:
        raise HTTPException(400, "local RAG is unavailable: pip install 'pwndoc-ai[rag]'")
    name = safe_filename(file.filename or "")
    if Path(name).suffix.lower() not in SUPPORTED:
        raise HTTPException(400, f"unsupported file type; use one of {sorted(SUPPORTED)}")
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file is larger than 20 MB")
    path = index.docs_dir / name
    path.write_bytes(data)
    try:
        chunks = index.add_file(path)
    except Exception as exc:  # noqa: BLE001 - unreadable/corrupt documents
        path.unlink(missing_ok=True)
        log.warning("could not index %s: %s", name, exc)
        raise HTTPException(
            400, f"could not index {name}: unreadable or unsupported document"
        ) from exc
    return {"name": name, "chunks": chunks}


@api.delete("/rag/documents/{name}")
def rag_delete(name: str, _: AdminDep, gw: GatewayDep) -> dict[str, Any]:
    index = gw.rag_index()
    if index is None:
        raise HTTPException(400, "local RAG is unavailable")
    index.remove(safe_filename(name))
    return {"deleted": safe_filename(name)}


@api.get("/audits/{audit_id}/findings")
def audit_findings(audit_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    if not SAFE_ID.match(audit_id):
        raise HTTPException(400, "invalid audit id")
    try:
        with gw.client(identity.cookies) as client:
            findings = client.list_findings(audit_id)
    except PwnDocError as exc:
        raise pwndoc_http_error(exc) from exc
    return {
        "findings": [
            {
                "id": str(f.get("_id") or f.get("id")),
                "title": f.get("title", ""),
                "severity": _severity(f),
            }
            for f in findings
        ]
    }


@api.post("/jobs")
def start_job(body: JobRequest, identity: UserDep, gw: GatewayDep) -> dict[str, str]:
    unknown = set(body.fields or ()) - set(ALL_FIELDS)
    if unknown:
        raise HTTPException(400, f"unknown fields: {', '.join(sorted(unknown))}")
    from .refiner import RefineOptions

    options = (
        RefineOptions(fields=tuple(body.fields), mode=body.mode)
        if body.fields
        else RefineOptions(mode=body.mode)
    )
    try:
        job = gw.jobs.start(
            identity.username,
            identity.cookies,
            body.audit_id,
            body.finding_ids,
            options,
            body.auto_apply,
        )
    except JobConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"job_id": job.id}


def _owned_job(job_id: str, identity: Identity, gw: Gateway) -> Any:
    try:
        return gw.jobs.get(job_id, identity.username)
    except JobNotFound as exc:
        raise HTTPException(404, "job not found") from exc


@api.get("/jobs/{job_id}")
def job_view(job_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    return _owned_job(job_id, identity, gw).view()


@api.post("/jobs/{job_id}/cancel")
def job_cancel(job_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    job = _owned_job(job_id, identity, gw)
    gw.jobs.cancel(job)
    return job.view()


@api.post("/jobs/{job_id}/apply")
def job_apply(job_id: str, body: ApplyRequest, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    job = _owned_job(job_id, identity, gw)
    try:
        gw.jobs.apply(job, identity.cookies, body.finding_ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except JobError as exc:
        log.warning("apply failed for job %s: %s", job_id, exc)
        raise HTTPException(500, "applying the changes failed; see the gateway log") from exc
    return job.view()


# -- app factory -----------------------------------------------------------------------------
def create_app(
    data_dir: str | os.PathLike[str] | None = None,
    transport: Any | None = None,
    refiner_factory: RefinerFactory | None = None,
    env: dict[str, str] | None = None,
) -> FastAPI:
    """Build the gateway. ``transport`` replaces the network for upstream calls (tests)."""
    gw = Gateway(ConfigStore(data_dir, env=env), transport, refiner_factory)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await gw.pool.aclose()

    app = FastAPI(title="pwndoc-ai gateway", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.gateway = gw
    app.add_middleware(ApiHeadersMiddleware)
    app.add_middleware(BodyLimitMiddleware)  # outermost of ours: reject before any parsing
    app.include_router(api)

    @app.exception_handler(UpstreamError)
    async def upstream_down(_: Request, exc: UpstreamError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.get("/__ai/static/{name}")
    def static_asset(name: str) -> FileResponse:
        media_type = STATIC_TYPES.get(name)
        path = STATIC_DIR / name
        if media_type is None or not path.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})

    @app.websocket("/socket.io/{rest:path}")
    async def socket_io(websocket: WebSocket, rest: str) -> None:
        await proxy_websocket(websocket, gw.store.settings)

    @app.api_route(
        "/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    )
    async def passthrough(request: Request, path: str):  # noqa: ANN202
        if path.startswith("__ai/"):
            raise HTTPException(404, "not found")
        return await proxy_http(request, gw.pool, gw.store.settings)

    return app


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _http_allowed(insecure_http: bool) -> bool:
    return insecure_http or os.environ.get(ALLOW_HTTP_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def run(
    host: str,
    port: int,
    data_dir: str | None = None,
    https: bool = False,
    insecure_http: bool = False,
) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if not https and not is_loopback(host) and not _http_allowed(insecure_http):
        raise InsecureBindError(
            f"refusing to serve plain HTTP on {host}: PwnDoc logins, session cookies and the LLM "
            f"API key would cross the network in clear text. Use --https, bind to 127.0.0.1 "
            f"behind a TLS terminator, or pass --insecure-http (or set {ALLOW_HTTP_ENV}=1) on a "
            "network you trust."
        )
    resolved = data_dir or os.environ.get("PWNDOC_AI_DATA_DIR", DEFAULT_DATA_DIR)
    os.umask(0o077)  # config, backups, uploads and exports hold client data: owner-only
    ssl_options: dict[str, str] = {}
    if https:
        certfile, keyfile = ensure_self_signed(resolved)
        ssl_options = {"ssl_certfile": certfile, "ssl_keyfile": keyfile}
    elif not is_loopback(host):
        log.warning("Serving plain HTTP on %s (explicitly allowed); use a trusted network.", host)
    # proxy_headers lets a TLS terminator in FORWARDED_ALLOW_IPS tell us the browser's scheme,
    # so the Secure cookie flag is only stripped when the browser really is on plain HTTP.
    uvicorn.run(create_app(resolved), host=host, port=port, proxy_headers=True, **ssl_options)
