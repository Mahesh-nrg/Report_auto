"""Request dependencies and helpers shared by the gateway's API routers.

Every per-audit route goes through :func:`audit_session` (or :func:`authorise_audit`) before it
does anything else: PwnDoc, with the user's own cookie, decides who may touch an audit, and no
limit, lock or log entry may answer for an audit the user cannot open.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request

from .gateway import Gateway, Identity
from .jobs import SAFE_ID
from .pwndoc_client import PwnDocClient, PwnDocError
from .throttle import Busy

log = logging.getLogger(__name__)

CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "pwndoc-ai"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PWNDOC_PASS_THROUGH = (401, 403, 404)


def gateway(request: Request) -> Gateway:
    return request.app.state.gateway


GatewayDep = Annotated[Gateway, Depends(gateway)]


def require_csrf_header(request: Request) -> None:
    """Only our own script sets this header, so a page on another site cannot send it."""
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(403, f"missing {CSRF_HEADER}: {CSRF_VALUE} header")


def csrf_guard(request: Request) -> None:
    """Router-level guard: every state-changing request needs the header."""
    if request.method not in SAFE_METHODS:
        require_csrf_header(request)


# A GET that costs real work (PwnDoc reads, a LibreOffice run) needs the header as well.
CostlyGet = Depends(require_csrf_header)


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
    if exc.status in PWNDOC_PASS_THROUGH:
        return HTTPException(exc.status, str(exc))
    log.warning("PwnDoc call failed: %s", exc)
    return HTTPException(502, "PwnDoc request failed; see the gateway log")


def check_audit_id(audit_id: str) -> None:
    if not SAFE_ID.fullmatch(audit_id):
        raise HTTPException(400, "invalid audit id")


@contextmanager
def audit_session(
    gw: Gateway, identity: Identity, audit_id: str
) -> Iterator[tuple[PwnDocClient, dict[str, Any]]]:
    """Open PwnDoc as the user and load the audit, so PwnDoc itself enforces access first.

    PwnDoc errors raised anywhere inside the ``with`` body become generic HTTP errors.
    """
    check_audit_id(audit_id)
    try:
        with gw.client(identity.cookies) as client:
            audit = client.get_audit(audit_id)
            if not isinstance(audit, dict):
                raise HTTPException(502, "PwnDoc returned an unexpected answer for the audit")
            yield client, audit
    except PwnDocError as exc:
        raise pwndoc_http_error(exc) from exc


def authorise_audit(gw: Gateway, identity: Identity, audit_id: str) -> None:
    """Ask PwnDoc, as the user, whether the audit can be opened (HTTP error when not)."""
    with audit_session(gw, identity, audit_id):
        pass


@contextmanager
def limited(gw: Gateway, name: str, key: str) -> Iterator[None]:
    """Hold a slot of the named limit, or answer 429 when the caller is over it."""
    try:
        with gw.limits[name].slot(key):
            yield
    except Busy as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": str(exc.retry_after)}) from exc
