"""Shared state of one running gateway: settings, upstream access, identities, jobs, RAG."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from .config import ConfigStore, Settings, effective_verify_tls
from .jobs import JobManager
from .llm import LlmClient
from .proxy import UpstreamPool
from .pwndoc_client import PwnDocClient, PwnDocError
from .rag import DocumentIndex, RagUnavailable

log = logging.getLogger(__name__)

IDENTITY_TTL = 30.0  # successful lookups
NEGATIVE_TTL = 5.0  # rejected tokens: stops bogus cookies from hammering PwnDoc
IDENTITY_CACHE_MAX = 1024
IDENTITY_TIMEOUT = 10.0
COOKIE_RE = r"(?:^|;\s*){name}=([^;]*)"
ADMIN_ROLE = "admin"

RefinerFactory = Callable[[Settings], Any]


class UpstreamError(RuntimeError):
    """PwnDoc could not be reached or answered with a server error (maps to HTTP 502)."""


@dataclass(frozen=True)
class Identity:
    username: str
    roles: tuple[str, ...]
    token: str  # raw (still URL-encoded) value of PwnDoc's ``token`` cookie

    @property
    def is_admin(self) -> bool:
        return ADMIN_ROLE in self.roles

    @property
    def role(self) -> str:
        """Single role for display: ``admin`` if held, otherwise the first one."""
        if self.is_admin:
            return ADMIN_ROLE
        return self.roles[0] if self.roles else ""

    @property
    def cookies(self) -> dict[str, str]:
        return {"token": self.token}


def raw_cookie(header: str, name: str) -> str | None:
    """Read a cookie without Starlette's URL-unquoting, so it can be forwarded unchanged."""
    match = re.search(COOKIE_RE.format(name=re.escape(name)), header or "")
    return match.group(1) if match else None


def parse_roles(me: dict[str, Any]) -> tuple[str, ...]:
    """PwnDoc >= 1.x returns ``roles`` (a list); older releases return ``role`` (a string)."""
    roles = me.get("roles")
    if isinstance(roles, list):
        return tuple(str(r) for r in roles if r)
    role = me.get("role")
    return (str(role),) if role else ()


class Gateway:
    """Shared state of one running gateway (settings, upstream access, jobs, RAG)."""

    def __init__(
        self,
        store: ConfigStore,
        transport: Any | None = None,
        refiner_factory: RefinerFactory | None = None,
    ):
        self.store = store
        self.transport = transport
        self.pool = UpstreamPool(transport)
        self._refiner_factory = refiner_factory or self._default_refiner
        self.rag: DocumentIndex | None = None
        self._rag_lock = threading.Lock()
        self._identities: dict[str, tuple[float, Identity | None]] = {}
        self._identity_lock = threading.Lock()
        self.jobs = JobManager(
            store.data_dir, lambda: self._refiner_factory(store.settings), self.client
        )

    def client(
        self, cookies: dict[str, str] | None = None, timeout: float | None = None
    ) -> PwnDocClient:
        settings = self.store.settings
        extra = {} if timeout is None else {"timeout": timeout}
        return PwnDocClient(
            settings.pwndoc_url,
            effective_verify_tls(settings),
            cookies=cookies,
            transport=self.transport,
            **extra,
        )

    def identity(self, request: Request) -> Identity | None:
        """Who is calling? ``None`` when not signed in; raises UpstreamError if PwnDoc is down."""
        token = raw_cookie(request.headers.get("cookie", ""), "token")
        if not token:
            return None
        now = time.monotonic()
        with self._identity_lock:
            cached = self._identities.get(token)
            if cached and cached[0] > now:
                return cached[1]
        found = self._lookup(token)
        self._remember(token, found, now)
        return found

    def _lookup(self, token: str) -> Identity | None:
        try:
            with self.client({"token": token}, timeout=IDENTITY_TIMEOUT) as client:
                me = client.me()
        except PwnDocError as exc:
            if exc.status is None or exc.status >= 500:
                log.warning("identity lookup failed: %s", exc)
                raise UpstreamError("PwnDoc is unreachable from the gateway") from exc
            return None
        if not isinstance(me, dict):
            log.warning("PwnDoc /api/users/me returned an unexpected payload")
            raise UpstreamError("unexpected answer from PwnDoc; check the PwnDoc URL setting")
        username = str(me.get("username", "")).strip()
        if not username:  # never let unnamed identities share one job-owner bucket
            return None
        return Identity(username, parse_roles(me), token)

    def _remember(self, token: str, found: Identity | None, now: float) -> None:
        expiry = now + (IDENTITY_TTL if found else NEGATIVE_TTL)
        with self._identity_lock:
            live = {k: v for k, v in self._identities.items() if v[0] > now}
            if len(live) >= IDENTITY_CACHE_MAX:  # bounded: bogus cookies cannot grow it forever
                oldest = sorted(live, key=lambda k: live[k][0])[: len(live) // 2]
                live = {k: v for k, v in live.items() if k not in oldest}
            self._identities = {**live, token: (expiry, found)}

    def rag_index(self) -> DocumentIndex | None:
        with self._rag_lock:
            if self.rag is None:
                try:
                    self.rag = DocumentIndex(self.store.data_dir / "rag")
                except RagUnavailable:
                    return None
                except Exception:  # noqa: BLE001 - a broken vector store must not crash the API
                    log.exception("could not open the document index")
                    return None
            return self.rag

    def _default_refiner(self, settings: Settings) -> Any:
        from .context import make_context_provider
        from .refiner import FindingRefiner

        rag = self.rag_index() if settings.use_rag else None
        return FindingRefiner(LlmClient(settings), settings, make_context_provider(settings, rag))
