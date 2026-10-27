"""Background refinement runs over uploaded Word reports.

One worker thread per run; a user has at most one active run and the server at most two. The
state machine lives in the document's ``meta.json`` (``uploaded`` -> ``running`` -> ``done`` /
``error`` / ``cancelled``): it is written *before* the work starts, so a gateway restart shows a
dead run as ``error: interrupted`` (see :class:`pwndoc_ai.docx_store.DocxStore`), and the refined
files, the change list and the summary are stored together, only when the run finished.

Cancelling: the engine's cancel event is set. Whether the engine returns what it has or raises
``RefineCancelled``, a cancelled run keeps only the original and ends ``cancelled``; nothing is
partially stored. An LLM authentication error stops the run early (every further chunk would fail
the same way) and ends ``error`` with the fixed "key rejected" sentence.

Document text is untrusted data and client data: it never reaches a log, the run log or an error.
Failures are logged by exception type only, and users only ever see fixed sentences (or the
engine's own user-safe ``DocxError`` text, cleaned and capped).
"""

from __future__ import annotations

import importlib
import logging
import re
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .config import Settings
from .docx_store import DocxNotFound, DocxRecord, DocxStore
from .job_support import LLM_FAILED, LLM_KEY_REJECTED, UNEXPECTED, user_message
from .llm import LlmAuthError
from .masking import Masker, NullMasker
from .reports import is_docx
from .uploads import UploadError

if TYPE_CHECKING:
    from .history import RunLog

log = logging.getLogger(__name__)

MODES = ("light", "deep")
MAX_RUNNING_PER_USER = 1
MAX_RUNNING_TOTAL = 2
MAX_SECTION_IDS = 500
MAX_TERMS = 50
MAX_TERM_CHARS = 200
MAX_ERROR_CHARS = 200
MAX_CURRENT_CHARS = 200
MAX_MODELS = 10
JOIN_SECONDS = 5.0  # how long shutdown waits for runs that were told to stop
SECTION_ID = re.compile(r"s\d{1,5}")
CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
ENGINE_MODULE = "pwndoc_ai.docx_engine"
NO_LLM = "no LLM is configured: an administrator must set it in AI Settings"
NOT_INSTALLED = "document refinement is not installed: pip install 'pwndoc-ai[docx]'"
NOT_PRODUCED = "the refined document could not be produced"
NOT_STARTED = "the server could not start the run"
BUSY_USER = "you already have a refinement running; wait for it or cancel it"
BUSY_FILE = "this document is still being refined; cancel the run first"
BUSY_SERVER = "the server is busy with other refinements; try again shortly"


class DocxJobError(ValueError):
    """A refused request; the message is a fixed, user-safe sentence."""


class NoLlmError(DocxJobError):
    """No LLM is configured (HTTP 400)."""


class ConflictError(RuntimeError):
    """A run is already active (HTTP 409); the message is user-safe."""


class EngineUnavailable(RuntimeError):
    """The refine engine (or python-docx / lxml) is not installed (HTTP 501)."""


def load_engine() -> Any:
    """The engine package, imported on first use so the gateway starts without the extra."""
    try:
        return importlib.import_module(ENGINE_MODULE)
    except ImportError as exc:
        log.warning("docx engine unavailable (%s)", type(exc).__name__)
        raise EngineUnavailable(NOT_INSTALLED) from exc


def clean_text(value: Any, limit: int) -> str:
    """One line of plain text: control characters become spaces, then cut to ``limit``."""
    return CONTROL.sub(" ", str(value or "")).strip()[:limit]


@dataclass(frozen=True)
class RunOptions:
    """A validated refine request (plain values; the engine's own request is built from it)."""

    mode: str
    section_ids: tuple[str, ...] | None = None
    client_terms: tuple[str, ...] = ()


def validate_options(
    mode: Any, section_ids: Any, client_terms: Any, outline: Iterable[Mapping[str, Any]]
) -> RunOptions:
    """Check a request against the document's outline; :class:`DocxJobError` when it is bad."""
    if mode not in MODES:
        raise DocxJobError("the mode must be 'light' or 'deep'")
    known = {str(o.get("id")) for o in outline}
    sections = None
    if section_ids is not None:
        if not isinstance(section_ids, list | tuple) or not 0 < len(section_ids) <= MAX_SECTION_IDS:
            raise DocxJobError("pick at least one section, or refine the whole document")
        if not all(isinstance(i, str) for i in section_ids):  # before hashing: [[1]] is a 400
            raise DocxJobError("unknown section")
        ids = tuple(dict.fromkeys(section_ids))
        if not all(SECTION_ID.fullmatch(i) and i in known for i in ids):
            raise DocxJobError("unknown section")
        sections = ids
    return RunOptions(mode, sections, _clean_terms(client_terms))


def _clean_terms(terms: Any) -> tuple[str, ...]:
    if not isinstance(terms, list | tuple) or len(terms) > MAX_TERMS:
        raise DocxJobError("too many names to mask")
    cleaned = []
    for term in terms:
        if not isinstance(term, str) or CONTROL.search(term):
            raise DocxJobError("a name to mask is not valid")
        text = term.strip()
        if not 0 < len(text) <= MAX_TERM_CHARS:
            raise DocxJobError("a name to mask is empty or too long")
        cleaned.append(text)
    return tuple(dict.fromkeys(cleaned))


def masker_factory_for(settings: Settings) -> Callable[[tuple[str, ...]], Any]:
    """Masker per run: everything the user named, plus IPs/e-mails/URLs when masking is on."""

    def build(terms: tuple[str, ...]) -> Any:
        if settings.mask_sensitive or terms:
            return Masker(("CLIENT", term) for term in terms)
        return NullMasker()

    return build


class _GuardedLlm:
    """Passes calls through; an authentication failure stops the whole run (it would repeat)."""

    def __init__(self, llm: Any, on_auth_error: Callable[[], None]):
        self._llm = llm
        self._on_auth_error = on_auth_error

    @property
    def last_model(self) -> str:
        return str(getattr(self._llm, "last_model", ""))

    def call_tool(self, system: str, user: str, tool: dict[str, Any]) -> Any:
        try:
            return self._llm.call_tool(system, user, tool)
        except LlmAuthError:
            self._on_auth_error()
            raise


@dataclass(frozen=True)
class _Outcome:
    state: str  # done | error | cancelled
    error: str = ""
    clean: bytes = b""
    tracked: bytes = b""
    changes: tuple[dict[str, Any], ...] = ()
    stats: Mapping[str, Any] = field(default_factory=dict)


class _Job:
    def __init__(self, owner: str, file_id: str, options: RunOptions, started: str):
        self.id = uuid.uuid4().hex
        self.owner, self.file_id, self.options, self.started = owner, file_id, options, started
        self.cancel = threading.Event()
        self.auth_failed = threading.Event()
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._progress = {"total": 0, "done": 0, "changed": 0, "rejected": 0, "protected": 0}
        self._current = ""

    def update(self, progress: Any) -> None:
        """Copy the numbers out of the engine's progress object (it may reuse and mutate it)."""
        values = {
            "total": _count(getattr(progress, "total_chunks", 0)),
            "done": _count(getattr(progress, "done_chunks", 0)),
            "changed": _count(getattr(progress, "changed", 0)),
            "rejected": _count(getattr(progress, "rejected", 0)),
            "protected": _count(getattr(progress, "protected", 0)),
        }
        current = clean_text(getattr(progress, "current", ""), MAX_CURRENT_CHARS)
        with self._lock:
            self._progress, self._current = values, current

    def view(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": "running",
                **self._progress,
                "current": self._current,
                "error": "",
                "started": self.started,
                "ended": None,
            }


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DocxRefineJobs:
    def __init__(
        self,
        store: DocxStore,
        *,
        engine: Callable[[], Any],
        llm_factory: Callable[[Settings], Any],
        context_factory: Callable[[Settings], Callable[..., Any]],
        settings_provider: Callable[[], Settings],
        runlog: RunLog | None = None,
        max_per_user: int = MAX_RUNNING_PER_USER,
        max_total: int = MAX_RUNNING_TOTAL,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self._store = store
        self._engine = engine
        self._llm_factory = llm_factory
        self._context_factory = context_factory
        self._settings = settings_provider
        self._runlog = runlog
        self._max_per_user = max_per_user
        self._max_total = max_total
        self._clock = clock
        self._jobs: dict[tuple[str, str], _Job] = {}
        self._lock = threading.Lock()

    # -- API --------------------------------------------------------------------------------
    def start(
        self,
        owner: str,
        file_id: str,
        *,
        mode: Any,
        section_ids: Any = None,
        client_terms: Any = (),
    ) -> None:
        """Validate, reserve the user's slot, persist ``running`` and start the worker thread.

        Raises :class:`DocxNotFound`, :class:`DocxJobError` (:class:`NoLlmError`),
        :class:`EngineUnavailable` or :class:`ConflictError`; nothing has changed then.
        """
        record = self._store.get(owner, file_id)
        options = validate_options(mode, section_ids, client_terms, record.outline)
        settings = self._settings()
        if not settings.llm_configured:
            raise NoLlmError(NO_LLM)
        engine = self._engine()
        job = _Job(owner, file_id, options, self._clock().isoformat())
        self._reserve(job)
        try:
            self._begin(job, record)
            self._launch(job, engine, settings)
        except BaseException:
            self._release(job)
            raise

    def status(self, owner: str, file_id: str) -> dict[str, Any]:
        """A fresh copy of the run's state (live numbers while it runs, the record afterwards)."""
        with self._lock:
            job = self._jobs.get((owner, file_id))
        if job is not None:
            return job.view()
        record = self._store.get(owner, file_id)
        return {
            "state": record.state,
            "total": record.total,
            "done": record.done,
            "changed": record.changed,
            "rejected": record.rejected,
            "protected": record.protected,
            "current": record.current,
            "error": record.error,
            "started": record.started,
            "ended": record.ended,
        }

    def cancel(self, owner: str, file_id: str) -> dict[str, Any]:
        self._store.get(owner, file_id)  # 404 for other users' and unknown files
        with self._lock:
            job = self._jobs.get((owner, file_id))
        if job is not None:
            job.cancel.set()
        return self.status(owner, file_id)

    def is_active(self, owner: str, file_id: str) -> bool:
        with self._lock:
            return (owner, file_id) in self._jobs

    def wait(self, owner: str, file_id: str, timeout: float) -> bool:
        """Block until the file's run is over (True) or ``timeout`` passes (False)."""
        with self._lock:
            job = self._jobs.get((owner, file_id))
        if job is not None and job.thread is not None:
            job.thread.join(timeout)
        return not self.is_active(owner, file_id)

    def close(self) -> None:
        """Tell every run to stop and give the workers a moment to record that."""
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel.set()
        for job in jobs:
            if job.thread is not None:
                job.thread.join(JOIN_SECONDS)

    # -- starting ---------------------------------------------------------------------------
    def _reserve(self, job: _Job) -> None:
        key = (job.owner, job.file_id)
        with self._lock:
            if key in self._jobs:
                raise ConflictError(BUSY_FILE)
            mine = [j for j in self._jobs.values() if j.owner == job.owner]
            if sum(1 for j in mine if not j.cancel.is_set()) >= self._max_per_user:
                raise ConflictError(BUSY_USER)
            if len(self._jobs) >= self._max_total:
                raise ConflictError(BUSY_SERVER)
            self._jobs[key] = job

    def _release(self, job: _Job) -> None:
        with self._lock:
            if self._jobs.get((job.owner, job.file_id)) is job:
                del self._jobs[(job.owner, job.file_id)]

    def _begin(self, job: _Job, record: DocxRecord) -> None:
        """Drop the earlier result and persist ``running`` before any work is done."""
        self._store.discard_results(job.owner, job.file_id)
        self._store.update(
            job.owner,
            job.file_id,
            state="running",
            mode=job.options.mode,
            changed=0,
            rejected=0,
            protected=int(record.stats.get("protected", 0)),
            error="",
            started=job.started,
            ended=None,
            total=0,
            done=0,
            current="",
            run={},
        )
        self._log(job, "start", mode=job.options.mode)

    def _launch(self, job: _Job, engine: Any, settings: Settings) -> None:
        job.thread = threading.Thread(
            target=self._run, args=(job, engine, settings), name=f"docx-{job.id[:8]}", daemon=True
        )
        try:
            job.thread.start()
        except RuntimeError as exc:  # no thread available: end the run instead of leaving it
            log.warning("docx run %s could not start a thread", job.id)
            self._persist_end(job, _Outcome("error", error=NOT_STARTED))
            raise DocxJobError(NOT_STARTED) from exc

    # -- the worker -------------------------------------------------------------------------
    def _run(self, job: _Job, engine: Any, settings: Settings) -> None:
        try:
            outcome = self._execute(job, engine, settings)
        except Exception as exc:  # noqa: BLE001 - any failure must end the run visibly
            outcome = self._failure(job, engine, exc)
        try:
            outcome = self._persist_end(job, outcome)
        finally:
            self._release(job)
            self._log_end(job, outcome)

    def _execute(self, job: _Job, engine: Any, settings: Settings) -> _Outcome:
        data = self._store.read_file(job.owner, job.file_id, "original")
        options = job.options
        request = engine.RefineRequest(
            mode=options.mode, section_ids=options.section_ids, client_terms=options.client_terms
        )
        context = self._context_factory(settings) if options.mode == "deep" else None
        llm = self._llm_factory(settings)
        try:
            result = engine.refine_docx(
                data,
                request,
                _GuardedLlm(llm, self._stop_on_auth_error(job)),
                context=context,
                masker_factory=masker_factory_for(settings),
                on_progress=job.update,
                cancel=job.cancel,
                timestamp=self._clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
                settings=settings,
            )
        finally:
            _close(llm)
        return self._outcome(job, result)

    @staticmethod
    def _stop_on_auth_error(job: _Job) -> Callable[[], None]:
        def stop() -> None:
            job.auth_failed.set()
            job.cancel.set()

        return stop

    def _outcome(self, job: _Job, result: Any) -> _Outcome:
        if job.auth_failed.is_set():
            return _Outcome("error", error=LLM_KEY_REJECTED)
        if job.cancel.is_set():
            return _Outcome("cancelled")
        clean, tracked = getattr(result, "clean", None), getattr(result, "tracked", None)
        limit = 2 * self._store.max_file_bytes
        if not all(is_docx(b) and len(b) <= limit for b in (clean, tracked)):
            log.warning("docx run %s: the engine produced no valid document", job.id)
            return _Outcome("error", error=NOT_PRODUCED)
        stats = result.stats if isinstance(result.stats, Mapping) else {}
        chunks, failed = _count(stats.get("chunks")), _count(stats.get("failed_chunks"))
        if chunks and failed >= chunks:  # nothing was refined: not a success
            return _Outcome("error", error=LLM_FAILED)
        changes = tuple(_change_dict(c) for c in getattr(result, "changes", ()))
        return _Outcome("done", "", clean, tracked, changes, stats)

    def _failure(self, job: _Job, engine: Any, exc: Exception) -> _Outcome:
        cancelled_type = getattr(engine, "RefineCancelled", None)
        if job.auth_failed.is_set():
            return _Outcome("error", error=LLM_KEY_REJECTED)
        if job.cancel.is_set() or (
            isinstance(cancelled_type, type) and isinstance(exc, cancelled_type)
        ):
            return _Outcome("cancelled")
        log.warning("docx run %s failed (%s)", job.id, type(exc).__name__)  # never the message
        docx_error = getattr(engine, "DocxError", None)
        if isinstance(docx_error, type) and isinstance(exc, docx_error):
            return _Outcome("error", error=clean_text(exc, MAX_ERROR_CHARS) or UNEXPECTED)
        return _Outcome("error", error=user_message(exc))

    # -- finishing --------------------------------------------------------------------------
    def _persist_end(self, job: _Job, outcome: _Outcome) -> _Outcome:
        """Write the final state (and the files of a finished run). Never raises."""
        ended = self._clock().isoformat()
        try:
            if outcome.state == "done":
                try:
                    self._save(job, outcome, ended)
                    return outcome
                except UploadError as exc:
                    outcome = _Outcome("error", error=clean_text(exc.message, MAX_ERROR_CHARS))
            self._store.update(
                job.owner,
                job.file_id,
                state=outcome.state,
                error=outcome.error,
                ended=ended,
                current="",
            )
        except DocxNotFound:
            log.warning("docx run %s: the document is gone", job.id)
        except OSError as exc:
            log.warning("docx run %s: could not store the outcome (%s)", job.id, type(exc).__name__)
            outcome = _Outcome("error", error=UNEXPECTED)
        return outcome

    def _save(self, job: _Job, outcome: _Outcome, ended: str) -> None:
        stats = outcome.stats
        chunks, failed = _count(stats.get("chunks")), _count(stats.get("failed_chunks"))
        models = [
            clean_text(m, 100) for m in list(stats.get("model_names") or [])[:MAX_MODELS] if m
        ]
        summary = {
            "changed": _count(stats.get("changed")),
            "rejected": _count(stats.get("rejected")),
            "protected": _count(stats.get("protected")),
        }
        record_fields = {
            **summary,
            "state": "done",
            "mode": job.options.mode,
            "error": "",
            "ended": ended,
            "total": chunks,
            "done": max(0, chunks - failed),
            "current": "",
            "run": {
                **summary,
                "unchanged": _count(stats.get("unchanged")),
                "chunks": chunks,
                "failed_chunks": failed,
                "models": models,
                "section_ids": list(job.options.section_ids or ()),
            },
        }
        self._store.save_result(
            job.owner, job.file_id, outcome.clean, outcome.tracked, outcome.changes, record_fields
        )

    # -- run log (kind 'docx'; ids and counts only, never a name or any text) -----------------
    def _log(self, job: _Job, event: str, **extra: Any) -> None:
        if self._runlog is None:
            return
        self._runlog.record(
            {
                "event": event,
                "job_id": job.id,
                "user": job.owner,
                "kind": "docx",
                "file_id": job.file_id,
                **extra,
            }
        )

    def _log_end(self, job: _Job, outcome: _Outcome) -> None:
        stats = outcome.stats
        self._log(
            job,
            "end",
            status=outcome.state,
            mode=job.options.mode,
            changed=_count(stats.get("changed")),
            rejected=_count(stats.get("rejected")),
            models=[clean_text(m, 100) for m in list(stats.get("model_names") or [])[:MAX_MODELS]],
            error=outcome.error,
        )


def _change_dict(change: Any) -> dict[str, Any]:
    """A plain dict of an engine ``Change`` (the store caps the texts)."""
    return {
        "unit_id": getattr(change, "unit_id", ""),
        "section": getattr(change, "section_id", ""),
        "before": getattr(change, "before", ""),
        "after": getattr(change, "after", ""),
        "status": getattr(change, "status", ""),
        "reason": getattr(change, "reason", ""),
    }


def _close(resource: Any) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        try:
            close()
        except Exception as exc:  # noqa: BLE001 - cleanup must never mask the outcome
            log.debug("closing the LLM client failed (%s)", type(exc).__name__)
