"""Shared state of one running gateway: settings, upstream access, identities, jobs, RAG."""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import Request

from .config import RESEARCH_CACHE_DIR, ConfigStore, Settings, effective_verify_tls
from .docx_import.service import ImportService
from .docx_import.store import ImportStore
from .docx_jobs import DocxRefineJobs
from .docx_pdf_cache import PdfCache
from .docx_store import DocxStore
from .embedder_host import EmbedderHost
from .export_service import ExportService
from .export_store import ExportStore
from .history import RunLog
from .indexer import RagIndexer
from .jobs import JobManager
from .llm import LlmClient
from .onnx_embedder import OnnxEmbedder
from .proxy import UpstreamPool
from .pwndoc_client import PwnDocClient, PwnDocError
from .rag import DocumentIndex, RagUnavailable
from .throttle import ResultCache, Throttle
from .uploads import DocumentStore

log = logging.getLogger(__name__)

IDENTITY_TTL = 30.0  # successful lookups
NEGATIVE_TTL = 5.0  # rejected tokens: stops bogus cookies from hammering PwnDoc
IDENTITY_CACHE_MAX = 1024
IDENTITY_TIMEOUT = 10.0
COOKIE_RE = r"(?:^|;\s*){name}=([^;]*)"
ADMIN_ROLE = "admin"
OWNER_ID_PREFIX = "id:"
ACCOUNT_ID = re.compile(r"[0-9a-fA-F]{24}")  # a MongoDB ObjectId, as PwnDoc's _id
IMPORT_DIR = "docx_import"  # <data_dir>/docx_import holds the DOCX importer's previews
EXPORT_DIR = "export"  # <data_dir>/export holds the Export tab's uploads and analyses
RAG_RETRY_SECONDS = 30.0  # after a failed index start, wait this long before the heavy retry

RefinerFactory = Callable[[Settings], Any]


@lru_cache(maxsize=1)
def rag_installed() -> bool:
    """Whether the optional vector-store dependency is importable (looked up once)."""
    return importlib.util.find_spec("chromadb") is not None


@lru_cache(maxsize=1)
def docx_installed() -> bool:
    """Whether python-docx (the refine engine's dependency) is importable (looked up once)."""
    return importlib.util.find_spec("docx") is not None


def _default_limits() -> dict[str, Throttle]:
    """Per-user limits for routes that spend LLM money or hold a worker thread for long."""
    return {
        "summary": Throttle(in_flight=1, per_minute=6, max_total=4),  # one LLM call each
        "qa": Throttle(in_flight=1, per_minute=30, max_total=8),  # may call LanguageTool
        "library": Throttle(in_flight=1, per_minute=10),
        "restore": Throttle(in_flight=1, per_minute=10),
        "restore_audit": Throttle(in_flight=1, per_minute=1000),  # keyed by audit, not user
        "history": Throttle(in_flight=2, per_minute=60),
        # document library: one upload at a time per admin and two across the server (disk, CPU)
        "rag_upload": Throttle(in_flight=1, per_minute=30, max_total=2),
        "rag_list": Throttle(in_flight=1, per_minute=30),  # reads every indexed chunk's metadata
        "rag_status": Throttle(in_flight=4, per_minute=300),  # polled while indexing runs
        "rag_admin": Throttle(in_flight=1, per_minute=30),  # start/cancel indexing, delete
        "rag_prepare": Throttle(in_flight=1, per_minute=6),  # may wait a while for a download
        # DOCX refine: all keyed by username, so one user can never use up another's allowance
        "docx_upload": Throttle(in_flight=1, per_minute=20, max_total=2),  # disk + a parse
        "docx_read": Throttle(in_flight=2, per_minute=120),  # list, detail, changes
        "docx_status": Throttle(in_flight=4, per_minute=300),  # polled while a run goes
        "docx_run": Throttle(in_flight=1, per_minute=30),  # start, cancel, delete
        "docx_download": Throttle(in_flight=2, per_minute=30, max_total=4),  # PDF = LibreOffice
        # DOCX import into PwnDoc: keyed by username as well
        "import_read": Throttle(in_flight=2, per_minute=120),  # list, preview, screenshots
        "import_status": Throttle(in_flight=4, per_minute=300),  # polled while a step runs
        "import_run": Throttle(in_flight=1, per_minute=30),  # analyse, cancel, delete
        "import_edit": Throttle(in_flight=2, per_minute=120),  # preview edits
        "import_write": Throttle(in_flight=1, per_minute=10),  # create and undo in PwnDoc
        # Export tab (fonts, sections, PDF import): keyed by username as well
        "export_upload": Throttle(in_flight=1, per_minute=20, max_total=2),  # upload, delete
        "export_read": Throttle(in_flight=2, per_minute=120),  # fonts, engines, list, detail
        "export_status": Throttle(in_flight=4, per_minute=300),  # polled while analysing
        "export_run": Throttle(in_flight=1, per_minute=30),  # analyse, cancel, plan
        "export_download": Throttle(in_flight=2, per_minute=30, max_total=4),  # LibreOffice
    }


def _export_service(store: ConfigStore) -> ExportService:
    """The Export tab service; it reads settings and the LLM per analysis (tests patch
    ``LlmClient`` in this module)."""
    return ExportService(
        ExportStore(store.data_dir / EXPORT_DIR),
        llm_factory=lambda settings: LlmClient(settings),
        settings=lambda: store.settings,
        data_dir=store.data_dir,
    )


class UpstreamError(RuntimeError):
    """PwnDoc could not be reached or answered with a server error (maps to HTTP 502)."""


@dataclass(frozen=True)
class Identity:
    username: str
    roles: tuple[str, ...]
    token: str  # raw (still URL-encoded) value of PwnDoc's ``token`` cookie
    uid: str = ""  # PwnDoc's immutable account id (24 hex digits), "" when it did not send one

    @property
    def owner(self) -> str:
        """What stored per-user data is keyed by: the account id, else the user name."""
        return f"{OWNER_ID_PREFIX}{self.uid}" if self.uid else self.username

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


def token_expiry(token: str) -> float:
    """The ``exp`` claim of PwnDoc's ``JWT <jwt>`` cookie value, or 0 when it cannot be read.

    PwnDoc has already accepted the token by the time this is called; the value only *orders*
    tokens (newest wins), it is never used to trust one.
    """
    try:
        payload = unquote(token).split(" ", 1)[-1].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims.get("exp", 0))
    except (IndexError, ValueError, TypeError, AttributeError):
        return 0.0


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
        self._rag_lock = threading.RLock()
        self._rag_failed_at: float | None = None  # monotonic time of the last failed start
        self._rag_built_with: Any = None  # the embedder the index we opened was built with
        self._rag_opened = False  # True when ``self.rag`` was opened here (not injected)
        self._indexer: RagIndexer | None = None
        self._documents: DocumentStore | None = None
        # one embedder per process, shared by the index, the research providers and the routes
        self.embedders = EmbedderHost(store, busy=self.indexing_now)
        # ranks web results when there is no Ollama (loaded on first use)
        self.onnx = OnnxEmbedder()
        self.clock: Callable[[], float] = time.monotonic
        self._identities: dict[str, tuple[float, Identity | None]] = {}
        self._latest_tokens: dict[str, str] = {}  # username -> newest verified token
        self._identity_lock = threading.Lock()
        self._adopted: set[tuple[str, str]] = set()  # (user name, owner key) already moved
        self.runlog = RunLog(store.data_dir)
        self.limits = _default_limits()
        self.qa_cache = ResultCache()  # LanguageTool answers, keyed by the text they were run on
        self.health_lock = threading.Lock()  # one "Fix automatically" at a time
        self._start_docx_services(store)
        self.export = _export_service(store)
        # settings are read per job, so changes made in the UI apply to the next run
        self.jobs = JobManager(
            store.data_dir,
            lambda: self._refiner_factory(store.settings),
            self.client,
            settings_provider=lambda: store.settings,
            runlog=self.runlog,
        )

    def _start_docx_services(self, store: ConfigStore) -> None:
        """The DOCX refine store, jobs and importer (late-bound: settings are read per run)."""
        self.docx_store = DocxStore(store.data_dir)
        self.docx_pdf_cache = PdfCache()
        # late-bound lambdas: settings, the embedder and the engine are read per run
        self.docx_jobs = DocxRefineJobs(
            self.docx_store,
            engine=lambda: self.docx_engine(),
            llm_factory=lambda settings: LlmClient(settings),
            context_factory=lambda settings: self.context_provider(settings),
            settings_provider=lambda: store.settings,
            runlog=self.runlog,
        )
        # the importer reads the LLM through this late-bound lambda (tests patch LlmClient)
        self.docx_import = ImportService(
            ImportStore(store.data_dir / IMPORT_DIR),
            self.docx_store,
            llm_factory=lambda settings: LlmClient(settings),
            settings=lambda: store.settings,
            runlog=self.runlog,
            engine=lambda: self.docx_engine(),
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
                self._track_token(token, cached[1])
                return cached[1]
        found = self._lookup(token)
        self._remember(token, found, now)
        with self._identity_lock:
            self._track_token(token, found)
        return found

    def _track_token(self, token: str, found: Identity | None) -> None:
        """Caller holds the identity lock. Keeps each user's newest token PwnDoc accepted."""
        if found is None:  # logged out / revoked: stop acting with it
            self._latest_tokens = {u: t for u, t in self._latest_tokens.items() if t != token}
            return
        current = self._latest_tokens.get(found.username)
        if current is None or token_expiry(token) >= token_expiry(current):
            self._latest_tokens[found.username] = token  # a stale tab cannot roll it back

    def session_cookies(self, username: str) -> dict[str, str] | None:
        """The newest PwnDoc session cookie seen for ``username``.

        PwnDoc rotates its short-lived token while the browser stays open, and the browser keeps
        talking to this gateway, so long-running jobs call this instead of holding the token they
        started with.
        """
        with self._identity_lock:
            token = self._latest_tokens.get(username)
        return {"token": token} if token else None

    def docx_owner(self, identity: Identity) -> str:
        """The storage owner of ``identity``'s DOCX files. The first time an account is seen,
        documents that were stored under its user name (before accounts were keyed by id) move to
        the account, so a user name freed later and given to somebody else finds nothing."""
        owner = identity.owner
        if owner == identity.username:
            return owner
        key = (identity.username, owner)
        with self._identity_lock:
            if key in self._adopted:
                return owner
        self.docx_store.adopt(identity.username, owner)
        with self._identity_lock:
            self._adopted.add(key)
        return owner

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
        raw_id = me.get("_id")
        uid = raw_id.lower() if isinstance(raw_id, str) and ACCOUNT_ID.fullmatch(raw_id) else ""
        return Identity(username, parse_roles(me), token, uid)

    def _remember(self, token: str, found: Identity | None, now: float) -> None:
        expiry = now + (IDENTITY_TTL if found else NEGATIVE_TTL)
        with self._identity_lock:
            live = {k: v for k, v in self._identities.items() if v[0] > now}
            if len(live) >= IDENTITY_CACHE_MAX:  # bounded: bogus cookies cannot grow it forever
                oldest = sorted(live, key=lambda k: live[k][0])[: len(live) // 2]
                live = {k: v for k, v in live.items() if k not in oldest}
            self._identities = {**live, token: (expiry, found)}

    def rag_index(self) -> DocumentIndex | None:
        """The document index, opened once. A failed start is not retried for a short while,
        so status polls do not repeat the heavy initialisation under the lock.

        When the embedder settings changed the index is reopened on the new embedder (a vector
        space has its own collection), but never while an indexing run is using it.
        """
        with self._rag_lock:
            embedder = self.embedders.get()
            if self.rag is not None and self._stale(embedder):
                self._close_index()
            if self.rag is not None:
                return self.rag
            failed = self._rag_failed_at
            if failed is not None and self.clock() - failed < RAG_RETRY_SECONDS:
                return None
            self._open_index(embedder)
            return self.rag

    def _stale(self, embedder: Any) -> bool:
        return self._rag_opened and embedder is not self._rag_built_with and not self.indexing_now()

    def _open_index(self, embedder: Any) -> None:
        path = self.store.data_dir / "rag"
        try:
            self.rag = (
                DocumentIndex(path) if embedder is None else DocumentIndex(path, embedder=embedder)
            )
            self._rag_opened, self._rag_built_with = True, embedder
        except RagUnavailable:
            self._rag_failed_at = self.clock()
        except Exception:  # noqa: BLE001 - a broken vector store must not crash the API
            log.exception("could not open the document index")
            self._rag_failed_at = self.clock()

    def _close_index(self) -> None:
        index, self.rag, self._rag_opened = self.rag, None, False
        self._documents = None
        close = getattr(index, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - replacing it: nothing useful to do about it
                log.exception("could not close the document index")

    def docs_dir(self) -> Path:
        """The documents folder of the open index (or where it will be)."""
        return Path(getattr(self.rag, "docs_dir", self.store.data_dir / "rag" / "docs"))

    def documents(self) -> DocumentStore | None:
        """Where uploads are stored (one store per folder, so its lock covers every upload)."""
        index = self.rag_index()
        if index is None:
            return None
        folder = Path(index.docs_dir)
        with self._rag_lock:
            if self._documents is None or self._documents.docs_dir != folder:
                self._documents = DocumentStore(folder)
            return self._documents

    def indexer(self) -> RagIndexer:
        """The process-wide indexing job (one run at a time, in a background thread)."""
        with self._rag_lock:
            if self._indexer is None:
                self._indexer = RagIndexer(self._index_for_run, self.docs_dir())
            return self._indexer

    def _index_for_run(self) -> DocumentIndex:
        index = self.rag_index()
        if index is None:
            raise RagUnavailable("the document index is not available")
        return index

    def indexing_now(self) -> bool:
        indexer = self._indexer
        return indexer is not None and indexer.status().get("state") == "running"

    def start_embedder_cleanup(self) -> None:
        """Build the embedder in the background: that stops servers a killed gateway left behind.

        Reaping can wait for a stopping server for a few seconds, so start-up does not wait for it.
        """

        def clean() -> None:
            try:
                self.embedders.get()
            except Exception:  # noqa: BLE001 - housekeeping must never take the gateway down
                log.exception("could not look for Ollama servers left behind")

        threading.Thread(target=clean, name="embedder-cleanup", daemon=True).start()

    def close_rag(self) -> None:
        """Stop an indexing run and the on-demand embedder server when the gateway shuts down."""
        with self._rag_lock:
            indexer, index = self._indexer, self.rag
        if indexer is not None:
            indexer.close()  # cancels a running job and waits for it (outside the lock)
        close = getattr(index, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - shutting down: nothing useful to do about it
                log.exception("could not stop the embedder")
        self.embedders.close()

    def context_provider(self, settings: Settings) -> Any:
        """The research provider (web + local books) shared by report and DOCX refine runs."""
        from .context import make_context_provider

        rag = self.rag_index() if settings.use_rag else None
        embedder = self.embedders.get()
        return make_context_provider(
            settings,
            rag,
            self.store.data_dir / RESEARCH_CACHE_DIR,
            embedder,
            fallback_embedder=self.onnx,  # ranks when there is no Ollama, or when it fails
        )

    def docx_engine(self) -> Any:
        """The DOCX refine engine package (EngineUnavailable when the extra is missing)."""
        from . import docx_jobs

        return docx_jobs.load_engine()

    def close_docx(self) -> None:
        """Tell running DOCX refinements to stop when the gateway shuts down."""
        self.docx_jobs.close()

    def close_docx_import(self) -> None:
        """Tell running DOCX import steps to stop (a create rolls back) when shutting down."""
        self.docx_import.close()

    def close_export(self) -> None:
        """Tell running Export analyses to stop when the gateway shuts down."""
        self.export.close()

    def _default_refiner(self, settings: Settings) -> Any:
        from .refiner import FindingRefiner

        return FindingRefiner(LlmClient(settings), settings, self.context_provider(settings))
