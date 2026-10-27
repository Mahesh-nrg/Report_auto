"""Routes for finding order, verification and normalisation (docs/FINDING_ORDER.md).

- ``GET  /audits/{id}/findings/order``  ordered view + proposed changes + verification + digest
- ``GET  /audits/{id}/findings/verification``  the B8 report only (JSON)
- ``POST /audits/{id}/normalize``  apply the reviewed plan (``digest``) behind backup + lock
- ``POST /audits/{id}/normalize/fill``  AI-first fill of empty fields (``fill_missing`` job,
  reviewed and applied through the normal job flow) - run it before ``normalize``

``download_normalise`` is the download route's hook: verify, and when the Settings switch is on
and the audit is not normalised, run the same backup-protected write first.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from .api_deps import GatewayDep, UserDep, audit_session, authorise_audit, csrf_guard
from .finding_apply import ApplyError, PlanChanged, apply_plan
from .finding_review import (
    Normalizer,
    NormalizeUnavailable,
    build_plan,
    header_value,
    plan_digest,
    verification,
)
from .jobs import JobConflict

log = logging.getLogger(__name__)
router = APIRouter(prefix="/__ai/api", dependencies=[Depends(csrf_guard)])
NO_LLM = "no LLM is configured: an administrator must set it in AI Settings"
FILL_FIELDS = ("description", "observation", "poc", "scope", "remediation", "references",
               "customFields")  # fmt: skip


class NormalizeRequest(BaseModel):
    digest: str = Field(default="", max_length=64)


def normalizer_of(gw: Any) -> Normalizer | None:
    """A stand-in set on the gateway (tests, integrator wiring) or the default module."""
    return getattr(gw, "finding_normalizer", None)


def _plan(findings: Any, gw: Any) -> list[Any]:
    try:
        return build_plan(findings if isinstance(findings, list) else [], normalizer_of(gw))
    except NormalizeUnavailable as exc:
        raise HTTPException(501, str(exc)) from exc


@router.get("/audits/{audit_id}/findings/order")
def findings_order(audit_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    with audit_session(gw, identity, audit_id) as (_, audit):
        plan = _plan(audit.get("findings"), gw)
    return {
        "findings": [row.view() for row in plan],
        "digest": plan_digest(plan),
        "verification": verification(audit_id, plan),
        "normalise_before_download": gw.store.settings.normalise_before_download,
    }


@router.get("/audits/{audit_id}/findings/verification")
def findings_verification(audit_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, Any]:
    with audit_session(gw, identity, audit_id) as (_, audit):
        plan = _plan(audit.get("findings"), gw)
    return verification(audit_id, plan)


@router.post("/audits/{audit_id}/normalize")
def normalize_audit(
    audit_id: str,
    identity: UserDep,
    gw: GatewayDep,
    body: NormalizeRequest = Body(default_factory=NormalizeRequest),  # noqa: B008
) -> dict[str, Any]:
    """Apply the plan the user reviewed; a different live plan is refused (409)."""
    with audit_session(gw, identity, audit_id) as (client, _):
        try:
            return apply_plan(
                gw.store.data_dir, audit_id, client, gw.jobs, normalizer_of(gw), body.digest or None
            )
        except NormalizeUnavailable as exc:
            raise HTTPException(501, str(exc)) from exc
        except (JobConflict, PlanChanged) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ApplyError as exc:
            undo = "the backup was restored" if exc.restored else f"restore backup {exc.backup}"
            raise HTTPException(502, f"{exc}; {undo}") from exc


@router.post("/audits/{audit_id}/normalize/fill")
def normalize_fill(audit_id: str, identity: UserDep, gw: GatewayDep) -> dict[str, str]:
    """AI first (B9): ask the model only for the empty fields; the user reviews and applies."""
    authorise_audit(gw, identity, audit_id)
    if not gw.store.settings.llm_configured:
        raise HTTPException(400, NO_LLM)
    from .refiner import RefineOptions

    options = RefineOptions(fields=FILL_FIELDS, mode="fill_missing")
    try:
        job = gw.jobs.start(identity.username, identity.cookies, audit_id, None, options, False)
    except JobConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"job_id": job.id}


def _skip(reason: str, report: dict[str, Any]) -> tuple[dict[str, Any], str]:
    return report, reason


def download_normalise(gw: Any, client: Any, audit_id: str, audit: dict[str, Any]) -> tuple:
    """Verify before a download; maybe normalise. Returns (report or None, outcome word).

    Outcomes: ``ok`` (already normalised), ``written``, ``off`` (switch off), ``busy``,
    ``refused`` (PwnDoc refused the write, e.g. a read-only user; rolled back), ``unavailable``.
    A failure never blocks the download: the report is exported as stored.
    """
    normalizer = normalizer_of(gw)
    try:
        plan = build_plan(audit.get("findings") or [], normalizer)
    except NormalizeUnavailable:
        return None, "unavailable"
    report = verification(audit_id, plan)
    if report["normalised"]:
        return report, "ok"
    if not gw.store.settings.normalise_before_download:
        return _skip("off", report)
    try:
        result = apply_plan(gw.store.data_dir, audit_id, client, gw.jobs, normalizer)
    except JobConflict:
        return _skip("busy", report)
    except (ApplyError, PlanChanged) as exc:
        log.warning("download: normalising audit %s failed (%s)", audit_id, type(exc).__name__)
        return _skip("refused", report)
    return result["report"], "written"


def download_headers(report: dict[str, Any] | None, outcome: str) -> dict[str, str]:
    verified = "unavailable" if report is None else header_value(report)
    return {"X-Findings-Verified": verified, "X-Findings-Normalised": outcome}
