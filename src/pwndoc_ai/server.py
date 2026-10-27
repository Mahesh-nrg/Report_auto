"""FastAPI gateway: reverse-proxies PwnDoc, serves the injected UI assets and the AI API."""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

import uvicorn
from fastapi import (
    APIRouter,
    Body,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import __version__
from .api_deps import (  # noqa: F401 - the dependencies are part of this module's API
    CSRF_HEADER,
    CSRF_VALUE,
    SAFE_METHODS,
    AdminDep,
    CostlyGet,
    GatewayDep,
    UserDep,
    audit_session,
    authorise_audit,
    check_audit_id,
    csrf_guard,
    pwndoc_http_error,
)
from .config import DEFAULT_DATA_DIR, ConfigStore, SettingsError
from .export_toolkit import pdf2docx_installed, pdf_tools_present
from .gateway import (  # noqa: F401 - re-exported
    Gateway,
    Identity,
    RefinerFactory,
    UpstreamError,
    docx_installed,
    rag_installed,
    raw_cookie,
)
from .jobs import SAFE_ID, JobConflict, JobError, JobNotFound, SessionExpiredError
from .layout_service import (
    PdfOutcome,
    layout_headers,
    layout_limited,
    parse_tidy,
    render_pdf,
    tidy_for_download,
)
from .limits import ApiHeadersMiddleware
from .llm import LlmClient, LlmError
from .pdf import PdfBusy, PdfError, PdfUnavailable
from .prompts import ALL_FIELDS
from .proxy import proxy_http, proxy_websocket
from .reports import FORMATS, content_disposition, export_blocker, is_docx, report_stem
from .server_docx import router as docx_router
from .server_export import ExportBodyLimit
from .server_export import router as export_router
from .server_features import router as features_router
from .server_findings import download_headers, download_normalise
from .server_findings import router as findings_router
from .server_import import router as import_router
from .server_rag import router as rag_router
from .tls import ensure_self_signed
from .typography_service import apply_defaults

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
# Changing these from the UI would let any PwnDoc admin redirect every user's session cookie.
LOCKED_SETTINGS = ("pwndoc_url", "verify_tls")
# Only plain asset names: no path separators, no dots but the extension, js and css only.
STATIC_NAME = re.compile(r"[a-z0-9_-]+\.(js|css)")
STATIC_TYPES = {"js": "text/javascript", "css": "text/css"}
MAX_FINDING_IDS = 500
LOOPBACK_NAMES = frozenset({"localhost"})
ALLOW_HTTP_ENV = "PWNDOC_AI_ALLOW_HTTP"
NO_LLM = "no LLM is configured: an administrator must set it in AI Settings"


class InsecureBindError(RuntimeError):
    """Refusing to serve plain HTTP on a non-loopback address without explicit consent."""


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
        "llm_model": gw.store.settings.llm_model,  # not a secret; the UI marks fallback answers
        "rag_available": rag_installed(),
        "docx_refine": docx_installed(),  # python-docx is importable: the refine engine can load
        "docx_import": docx_installed(),  # the importer reads with the same engine
        "export_tab": docx_installed(),  # fonts, sections and PDF import (Export tab)
        "pdf_import": pdf_tools_present(),  # mutool + pdftotext: the structure engine
        "pdf2docx": pdf2docx_installed(),  # the optional AGPL-licensed engine
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


@api.get("/audits/{audit_id}/findings")
def audit_findings(audit_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    with audit_session(gw, identity, audit_id) as (_, audit):
        findings = audit.get("findings")
    rows = [
        {
            "id": str(f.get("_id") or f.get("id")),
            "title": f.get("title", ""),
            "severity": _severity(f),
        }
        for f in (findings if isinstance(findings, list) else [])
        if isinstance(f, dict)
    ]
    return {"findings": _with_order(rows, findings, gw)}


def _with_order(rows: list[dict[str, Any]], findings: Any, gw: Gateway) -> list[dict[str, Any]]:
    """Stored order kept; report number (``seq``) and derived severity added as new keys."""
    from .finding_review import NormalizeUnavailable, build_plan
    from .server_findings import normalizer_of

    try:
        plan = build_plan(findings if isinstance(findings, list) else [], normalizer_of(gw))
    except NormalizeUnavailable:
        return rows
    views = {r.finding_id: r.view() for r in plan}
    keys = ("seq", "score", "mismatch", "estimated", "vector")
    return [
        {**row, **{k: views[row["id"]][k] for k in keys},
         "severity_derived": views[row["id"]]["severity"]}
        if row["id"] in views
        else row
        for row in rows
    ]  # fmt: skip


@api.post("/jobs")
def start_job(body: JobRequest, identity: UserDep, gw: GatewayDep) -> dict[str, str]:
    unknown = set(body.fields or ()) - set(ALL_FIELDS)
    if unknown:
        raise HTTPException(400, f"unknown fields: {', '.join(sorted(unknown))}")
    from .refiner import RefineOptions

    authorise_audit(gw, identity, body.audit_id)  # PwnDoc decides before any slot is taken
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


@api.post("/audits/{audit_id}/finish")
def finish_report(audit_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, str]:
    """Rewrite every finding of the audit with the AI, save them, then check the report builds."""
    authorise_audit(gw, identity, audit_id)  # PwnDoc decides before any slot is taken
    if not gw.store.settings.llm_configured:
        raise HTTPException(400, NO_LLM)
    from .refiner import RefineOptions

    def current_session() -> dict[str, str]:
        return gw.session_cookies(identity.username) or identity.cookies

    try:
        job = gw.jobs.start(
            identity.username,
            current_session,
            audit_id,
            None,
            RefineOptions(mode="improve"),
            auto_apply=True,
            export_report=True,
        )
    except JobConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"job_id": job.id}


@api.get("/audits/{audit_id}/report", dependencies=[CostlyGet])
def audit_report(
    audit_id: str,
    identity: UserDep,
    gw: GatewayDep,
    fmt: Annotated[Literal["docx", "pdf"], Query(alias="format")] = "docx",
    tidy: str = "1",
) -> Response:
    """The audit's report as Word or PDF, built on demand from PwnDoc (nothing is stored).

    A GET, but it costs a PwnDoc build and maybe a LibreOffice run: like the POST routes it must
    come from our own script, so a page on another site cannot trigger it with the user's cookie.
    The layout is tidied on the way out (``tidy=0`` opts out): blank pages, orphan headings and
    runs of empty paragraphs are fixed, a PDF is rendered with metric-compatible fonts.
    """
    tidy_on = parse_tidy(tidy)
    with audit_session(gw, identity, audit_id) as (client, audit):
        blocker = export_blocker(audit)
        if blocker:
            raise HTTPException(409, blocker)
        verified = download_headers(*download_normalise(gw, client, audit_id, audit))
        data = client.export_docx(audit_id)
    if not is_docx(data):
        raise HTTPException(502, "PwnDoc did not return a Word file")
    # the admin's default fonts first: tidy measures and the renderer draws the final fonts
    data, typography = apply_defaults(data, gw.store.settings)
    with layout_limited(gw, identity.username):
        if fmt == "pdf":
            outcome = _to_pdf(data, audit_id, tidy_on)
            body, layout = outcome.data, layout_headers(outcome.fix_count, outcome)
        else:
            tidied = tidy_for_download(data, enabled=tidy_on)
            body, layout = tidied.data, layout_headers(tidied.fix_count)
    extension, media_type = FORMATS[fmt]
    stem = report_stem(audit.get("name"))
    headers = {
        "Content-Disposition": content_disposition(stem, extension),
        **typography,
        **layout,
        **verified,
    }
    return Response(body, media_type=media_type, headers=headers)


def _to_pdf(docx: bytes, audit_id: str, tidy: bool) -> PdfOutcome:
    try:
        return render_pdf(docx, tidy=tidy)
    except PdfUnavailable as exc:
        raise HTTPException(501, str(exc)) from exc
    except PdfBusy as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": "10"}) from exc
    except PdfError as exc:
        log.warning("PDF conversion failed for audit %s: %s", audit_id, exc)
        raise HTTPException(502, "PDF conversion failed; see the gateway log") from exc


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
    except SessionExpiredError as exc:  # a fixed text; the user must sign in again
        raise HTTPException(401, str(exc)) from exc
    except JobError as exc:
        log.warning("apply failed for job %s: %s", job_id, exc)
        raise HTTPException(500, "applying the changes failed; see the gateway log") from exc
    return job.view()


# -- app factory -----------------------------------------------------------------------------
def _static_path(name: str) -> Path | None:
    """The asset file for a plain ``name.js`` / ``name.css`` that really lives in the static dir."""
    if not STATIC_NAME.fullmatch(name):
        return None
    root = STATIC_DIR.resolve()
    path = (STATIC_DIR / name).resolve()  # follows symlinks: one pointing outside is refused
    return path if path.is_file() and path.parent == root else None


def _lifespan(gw: Gateway) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        gw.start_embedder_cleanup()  # a killed gateway left its Ollama running: stop it now
        yield
        try:
            await gw.pool.aclose()
        finally:
            try:
                gw.close_docx()  # running refinements are told to stop and record it
                gw.close_docx_import()  # running import steps too (a create rolls back)
                gw.close_export()  # running Export analyses too
            finally:
                gw.close_rag()  # the embedder must stop even if closing the pool failed

    return lifespan


def create_app(
    data_dir: str | os.PathLike[str] | None = None,
    transport: Any | None = None,
    refiner_factory: RefinerFactory | None = None,
    env: dict[str, str] | None = None,
) -> FastAPI:
    """Build the gateway. ``transport`` replaces the network for upstream calls (tests)."""
    gw = Gateway(ConfigStore(data_dir, env=env), transport, refiner_factory)
    app = FastAPI(title="pwndoc-ai gateway", lifespan=_lifespan(gw), docs_url=None, redoc_url=None)
    app.state.gateway = gw
    app.add_middleware(ApiHeadersMiddleware)
    app.add_middleware(ExportBodyLimit)  # outermost of ours: reject before any parsing
    app.include_router(api)
    app.include_router(features_router)
    app.include_router(findings_router)
    app.include_router(rag_router)
    app.include_router(export_router)  # before the docx routes: a narrower prefix
    app.include_router(docx_router)
    app.include_router(import_router)

    @app.exception_handler(UpstreamError)
    async def upstream_down(_: Request, exc: UpstreamError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.get("/__ai/static/{name}")
    def static_asset(name: str) -> FileResponse:
        path = _static_path(name)
        if path is None:
            raise HTTPException(404, "not found")
        media_type = STATIC_TYPES[path.suffix.lstrip(".")]
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
