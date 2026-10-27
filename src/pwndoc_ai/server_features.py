"""Report-quality routes: QA, layout check, executive summary, library, history, restore, health.

Every per-audit route asks PwnDoc first (as the user) whether the audit can be opened; only then
does it take a rate-limit slot keyed by the audit, a lock, or touch any state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict
from typing import Annotated, Any, NamedTuple

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from .api_deps import (
    AdminDep,
    CostlyGet,
    GatewayDep,
    UserDep,
    audit_session,
    check_audit_id,
    csrf_guard,
    limited,
    pwndoc_http_error,
)
from .gateway import Gateway
from .health import HealthFixError, apply_fixes, run_checks
from .history import BackupError, list_backups, restore_backup
from .job_support import DEFAULT_LOCALE, JobConflict
from .layout_service import check_payload, layout_limited
from .library import save_findings
from .llm import LlmClient, LlmError
from .masking import Masker, build_masker
from .pdf import find_soffice
from .pwndoc_client import PwnDocClient, PwnDocError
from .qa import Issue, check_spelling, lint_audit, summarize
from .reports import export_blocker, is_docx
from .summary import (
    SummaryError,
    SummaryExistsError,
    build_stats,
    clean_summary_html,
    draft_summary,
    find_target,
    write_summary,
)
from .typography_service import apply_defaults

log = logging.getLogger(__name__)

# LanguageTool wants a regional code for these; PwnDoc stores the bare language.
LANGUAGETOOL_DEFAULTS = {"en": "en-US", "de": "de-DE", "pt": "pt-PT"}
LANGUAGE_CODE = re.compile(r"[a-z]{2,3}(?:-[A-Za-z]{2,4})?")
MAX_SUMMARY_CHARS = 100_000
SUMMARY_BACKUP_DIR = "summary-backups"
NO_LLM = "no LLM is configured: an administrator must set it in AI Settings"
SPELLING_UNAVAILABLE = Issue(
    "info",
    "SPELLCHECK_UNAVAILABLE",
    "The spelling check could not run (the LanguageTool server is unavailable or not allowed).",
    "",
    "",
)

router = APIRouter(prefix="/__ai/api", dependencies=[Depends(csrf_guard)])


class SummaryRequest(BaseModel):
    """Optional body of POST /summary: replace an existing summary with a draft already shown."""

    overwrite: bool = False
    summary_html: str | None = Field(default=None, max_length=MAX_SUMMARY_CHARS)


def _audit_findings(audit: dict[str, Any]) -> list[dict[str, Any]]:
    findings = audit.get("findings")
    return [f for f in findings if isinstance(f, dict)] if isinstance(findings, list) else []


def _audit_locale(audit: dict[str, Any]) -> str:
    language = audit.get("language")
    return language.strip() if isinstance(language, str) and language.strip() else DEFAULT_LOCALE


def _spelling_language(audit: dict[str, Any]) -> str:
    """The audit language as a LanguageTool code; ``auto`` when it is not recognisable."""
    code = _audit_locale(audit).replace("_", "-")
    if not LANGUAGE_CODE.fullmatch(code.lower()):
        return "auto"
    language, _, region = code.partition("-")
    language = language.lower()
    return (
        f"{language}-{region.upper()}" if region else LANGUAGETOOL_DEFAULTS.get(language, language)
    )


def _plain(value: Any) -> Any:
    """JSON-ready copy: read-only mappings (a draft's stats) become dicts, tuples lists."""
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


# -- QA ---------------------------------------------------------------------------------------
def _spelling_issues(gw: Gateway, audit: dict[str, Any]) -> tuple[list[Issue], bool]:
    """LanguageTool results and whether the check was unavailable.

    A result is remembered while the findings' text and the server stay the same. A failed check
    (``None``) is never remembered: the next request asks the server again.
    """
    url = gw.store.settings.languagetool_url
    if not url:
        return [], False
    findings, language = _audit_findings(audit), _spelling_language(audit)
    text = json.dumps(findings, sort_keys=True, default=str)
    key = (url, language, hashlib.sha256(text.encode("utf-8")).hexdigest())
    cached = gw.qa_cache.get(key)
    if cached is not None:
        return cached, False
    result = check_spelling(findings, url, language=language)
    if result is None:
        return [], True
    gw.qa_cache.put(key, result)
    return result, False


@router.get("/audits/{audit_id}/qa", dependencies=[CostlyGet])
def audit_qa(audit_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    """Read-only report lint (plus a spelling pass when a LanguageTool server is configured)."""
    with limited(gw, "qa", identity.username):
        with audit_session(gw, identity, audit_id) as (_, audit):
            pass
        spelling, unavailable = _spelling_issues(gw, audit)
        issues = [*lint_audit(audit), *spelling, *([SPELLING_UNAVAILABLE] if unavailable else [])]
    return {
        "issues": [asdict(issue) for issue in issues],
        "counts": summarize(issues),
        "spelling_unavailable": unavailable,
    }


# -- layout check -----------------------------------------------------------------------------
@router.get("/audits/{audit_id}/report/check", dependencies=[CostlyGet])
def audit_report_check(audit_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    """Layout facts of the audit's Word report as PwnDoc builds it now (nothing is stored), and
    what the tidy done at download time would change. Read-only; builds the report like the
    download does, so it needs the same header and a template."""
    with audit_session(gw, identity, audit_id) as (client, audit):
        blocker = export_blocker(audit)
        if blocker:
            raise HTTPException(409, blocker)
        data = client.export_docx(audit_id)
    if not is_docx(data):
        raise HTTPException(502, "PwnDoc did not return a Word file")
    data, _ = apply_defaults(data, gw.store.settings)  # the fonts the download will have
    with layout_limited(gw, identity.username):
        return check_payload(data, tidy=True)


# -- executive summary ------------------------------------------------------------------------
def _draft(gw: Gateway, audit: dict[str, Any]) -> Any:
    settings = gw.store.settings
    llm = None
    try:
        llm = LlmClient(settings)
        masker = build_masker(audit, settings.mask_sensitive)
        return draft_summary(llm, audit, _audit_findings(audit), settings, masker)
    except (LlmError, SummaryError, ValueError) as exc:
        log.warning("executive summary failed: %s", exc)
        raise HTTPException(
            502, "the AI model could not draft the summary; see the gateway log"
        ) from exc
    finally:
        if llm is not None:
            llm.close()


class _Stored(NamedTuple):
    written: bool
    target: str | None
    exists: bool = False  # the field already had text and overwriting was not requested
    error: str = ""


def _store_summary(
    client: PwnDocClient, gw: Gateway, audit: dict[str, Any], html: str, overwrite: bool
) -> _Stored:
    """Write the draft into the audit's summary field. The draft is returned either way.

    An existing summary is only replaced on request, and its old text is saved first.
    """
    target = find_target(audit)
    if target is None:
        return _Stored(False, None)
    name = target.section_name or "summary"
    backup_dir = gw.store.data_dir / SUMMARY_BACKUP_DIR / str(audit.get("_id"))
    try:
        written = write_summary(client, audit, html, overwrite=overwrite, backup_dir=backup_dir)
        return _Stored(written, name)
    except SummaryExistsError:
        return _Stored(False, name, exists=True)
    except PwnDocError as exc:
        if exc.status == 401:
            raise
        log.warning("could not write the summary into audit %s: %s", audit.get("_id"), exc)
        reason = f"PwnDoc answered {exc.status}" if exc.status else "PwnDoc is unreachable"
        return _Stored(False, name, error=f"{reason}; the summary was not saved")
    except ValueError as exc:
        log.warning("summary was not written: %s", exc)
        return _Stored(False, name, error=str(exc))  # fixed, user-meaningful texts from summary


@router.post("/audits/{audit_id}/summary")
def audit_summary(
    audit_id: str,
    identity: UserDep,
    gw: GatewayDep,
    body: Annotated[SummaryRequest | None, Body()] = None,
) -> dict[str, Any]:
    """Draft the executive summary with the LLM and, when the field is empty, save it.

    A summary the audit already has is left alone unless ``overwrite`` is sent; sending the
    ``summary_html`` of a draft already shown writes that draft without another LLM call.
    """
    request = body or SummaryRequest()
    with (
        limited(gw, "summary", identity.username),
        audit_session(gw, identity, audit_id) as (client, audit),
    ):
        if request.summary_html is None and not gw.store.settings.llm_configured:
            raise HTTPException(400, NO_LLM)
        if request.summary_html is None:
            draft = _draft(gw, audit)
            html, stats, model = draft.html, _plain(draft.stats), draft.model
        else:
            html = clean_summary_html(request.summary_html)
            stats, model = build_stats(_audit_findings(audit)), ""
        stored = _store_summary(client, gw, audit, html, request.overwrite)
    return {
        "summary_html": html,
        "stats": stats,
        "written": stored.written,
        "target": stored.target,
        "exists": stored.exists,
        "error": stored.error,
        "model": model,
    }


# -- library ----------------------------------------------------------------------------------
@router.post("/audits/{audit_id}/library")
def audit_library(audit_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    """Add this audit's findings to PwnDoc's vulnerability library (existing titles are skipped).

    Findings that still name the client, a host, an IP or an e-mail are skipped with a reason:
    the library is shared with every PwnDoc user.
    """
    with (
        limited(gw, "library", identity.username),
        audit_session(gw, identity, audit_id) as (client, audit),
    ):
        try:
            result = save_findings(
                client, _audit_findings(audit), _audit_locale(audit), Masker.from_audit(audit)
            )
        except ValueError as exc:
            log.warning("library save refused for audit %s: %s", audit_id, exc)
            raise HTTPException(
                400, "could not save to the library: the audit language or a finding is invalid"
            ) from exc
    return {
        "created": list(result.created),
        "skipped": [{"title": title, "reason": reason} for title, reason in result.skipped],
    }


# -- history and restore ----------------------------------------------------------------------
@router.get("/audits/{audit_id}/history", dependencies=[CostlyGet])
def audit_history(audit_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    with limited(gw, "history", identity.username):
        with audit_session(gw, identity, audit_id):
            pass
        return {
            "runs": gw.runlog.runs_for_audit(audit_id, live_job_ids=gw.jobs.active_job_ids()),
            "backups": list_backups(gw.store.data_dir, audit_id),
        }


@contextmanager
def _audit_writer(gw: Gateway, audit_id: str):  # noqa: ANN202 - a generator context manager
    """Hold the audit against AI jobs (and other restores); 409 when one is active."""
    try:
        with gw.jobs.exclusive(audit_id):
            yield
    except JobConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/audits/{audit_id}/backups/{name}/restore")
def audit_restore(audit_id: str, name: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    """Write a backup's findings back (after saving the current state as a new backup)."""
    check_audit_id(audit_id)
    with (
        limited(gw, "restore", identity.username),
        audit_session(gw, identity, audit_id) as (client, _),  # PwnDoc authorises first
        limited(gw, "restore_audit", audit_id),  # keyed by audit: only after authorisation
        _audit_writer(gw, audit_id),
    ):
        try:
            result = restore_backup(gw.store.data_dir, audit_id, name, client)
        except BackupError as exc:
            raise HTTPException(400, str(exc)) from exc
    return {
        "restored": len(result.restored),
        "skipped": [{"id": fid, "reason": reason} for fid, reason in result.skipped],
        "notes": [{"id": fid, "reason": reason} for fid, reason in result.notes],
        "safety_backup": result.safety_backup,
    }


# -- health -----------------------------------------------------------------------------------
def _rag_document_count(gw: Gateway) -> int | None:
    index = gw.rag_index()
    if index is None:
        return None
    try:
        return len(index.list_documents())
    except Exception:  # noqa: BLE001 - a broken vector store must not break the health page
        log.exception("could not list the RAG documents")
        return None


def _health_checks(client: PwnDocClient, gw: Gateway) -> list[dict[str, Any]]:
    checks = run_checks(
        client,
        gw.store.settings,
        rag_documents=_rag_document_count(gw),
        soffice=find_soffice(),
    )
    return [asdict(check) for check in checks]


@router.get("/health", dependencies=[CostlyGet])
def health(identity: AdminDep, gw: GatewayDep) -> dict[str, Any]:
    try:
        with gw.client(identity.cookies) as client:
            return {"checks": _health_checks(client, gw)}
    except PwnDocError as exc:
        raise pwndoc_http_error(exc) from exc


@router.post("/health/fix")
def health_fix(identity: AdminDep, gw: GatewayDep) -> dict[str, Any]:
    # PwnDoc can only replace ALL audit types at once: never let two admins interleave that.
    if not gw.health_lock.acquire(blocking=False):
        raise HTTPException(409, "another automatic fix is already running; try again shortly")
    try:
        with gw.client(identity.cookies) as client:
            applied = apply_fixes(client, gw.store.data_dir)
            return {"applied": applied, "checks": _health_checks(client, gw)}
    except HealthFixError as exc:
        log.warning("automatic fix stopped: %s", exc)
        raise HTTPException(500, str(exc)) from exc
    except PwnDocError as exc:
        raise pwndoc_http_error(exc) from exc
    finally:
        gw.health_lock.release()
