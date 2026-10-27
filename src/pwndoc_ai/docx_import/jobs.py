"""Background steps of the DOCX importer: analyse, create in PwnDoc, undo.

One daemon thread per step. A user has at most ``limits.MAX_RUNNING_PER_USER`` steps running
(analyse, create and undo share the slot) and the server at most ``limits.MAX_RUNNING_TOTAL``.
The service persists the running status *before* a step starts and the final one in a
``finally``, so a restart reports ``interrupted`` (``ImportStore.mark_interrupted``).

The collaborators (reader, plan, fallback, lookups, writer, undo, journal) are reached through
:class:`Collaborators`, whose defaults import the real modules on first use. The reader tree
imports lxml, so nothing here imports it at module level; a missing engine is
:class:`ImportEngineMissing` (HTTP 501). Document text never reaches a log, the run log or a
status: failures are logged by exception type, users see fixed sentences only.
"""

from __future__ import annotations

import importlib
import logging
import re
import threading
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..docx_store import DocxNotFound
from . import limits
from .errors import (
    ImportCancelled,
    ImportConflict,
    ImportEngineMissing,
    ImportFailure,
    ImportNotFound,
    ImportUpstreamError,
)
from .model import CompletenessIssue, ImageBlob, ImportedReport, ImportPlan
from .store import ImportStore, utc_stamp

log = logging.getLogger(__name__)

PACKAGE = "pwndoc_ai.docx_import"
JOIN_SECONDS = 5.0
CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

READING = "Reading the document"
CHECKING = "Checking PwnDoc"
FILLING = "Filling gaps with AI"
COMPLETENESS = "Checking completeness"
ANALYSE_STEPS = 4

OpenClient = Callable[[], AbstractContextManager[Any]]


# -- collaborators ----------------------------------------------------------------------------
def _module(name: str) -> Any:
    try:
        return importlib.import_module(f"{PACKAGE}.{name}")
    except ImportError as exc:  # lxml, the docx engine or a module of the importer is missing
        log.warning("docx import module unavailable (%s)", type(exc).__name__)
        raise ImportEngineMissing() from exc


def _lazy(module: str, name: str) -> Callable[..., Any]:
    def call(*args: Any, **kwargs: Any) -> Any:
        return getattr(_module(module), name)(*args, **kwargs)

    call.__name__ = name
    return call


@dataclass(frozen=True)
class Collaborators:
    """The functions of the other importer modules the service calls (contract §a)."""

    read_report: Callable[..., ImportedReport]
    default_audit_name: Callable[[str], str]
    load_options: Callable[[Any], tuple[Any, Any]]
    check_names: Callable[[Any, str, str], Any]
    fill_gaps: Callable[..., Any]
    build_plan: Callable[..., ImportPlan]
    apply_edits: Callable[..., ImportPlan]
    blockers: Callable[[Sequence[CompletenessIssue]], tuple[CompletenessIssue, ...]]
    warnings: Callable[[Sequence[CompletenessIssue]], tuple[CompletenessIssue, ...]]
    create_import: Callable[..., Any]
    undo_import: Callable[..., Any]
    journal_writer: Callable[[Path, str], Any]
    read_journal: Callable[[Path], Any]
    write_journal: Callable[[Path, Any], None]


def default_collaborators() -> Collaborators:
    """The real modules, each imported the first time it is called."""
    return Collaborators(
        read_report=_lazy("reader", "read_report"),
        default_audit_name=_lazy("plan", "default_audit_name"),
        load_options=_lazy("lookups", "load_options"),
        check_names=_lazy("lookups", "check_names"),
        fill_gaps=_lazy("fallback", "fill_gaps"),
        build_plan=_lazy("plan", "build_plan"),
        apply_edits=_lazy("plan", "apply_edits"),
        blockers=_lazy("completeness", "blockers"),
        warnings=_lazy("completeness", "warnings"),
        create_import=_lazy("writer", "create_import"),
        undo_import=_lazy("undo", "undo_import"),
        journal_writer=_lazy("journal", "JournalWriter"),
        read_journal=_lazy("journal", "read_journal"),
        write_journal=_lazy("journal", "write_journal"),
    )


# -- one running step -------------------------------------------------------------------------
def _count(value: Any) -> int:
    ok = isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= limits.MAX_COUNT
    return value if ok else 0


class ImportJob:
    """A running step: its cancel event and its live progress (a fixed label and two counts)."""

    def __init__(self, owner: str, import_id: str, phase: str):
        self.run_id = uuid.uuid4().hex
        self.owner, self.import_id, self.phase = owner, import_id, phase
        self.cancel = threading.Event()
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._progress: tuple[str, int, int] = ("", 0, 0)

    def progress(self, label: str, done: int, total: int) -> None:
        current = CONTROL.sub(" ", str(label or ""))[: limits.MAX_CURRENT_CHARS]
        with self._lock:
            self._progress = (current, _count(done), _count(total))

    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def check(self) -> None:
        if self.cancel.is_set():
            raise ImportCancelled()

    def overlay(self, status: Any) -> Any:
        """``status`` with the live progress of this step."""
        with self._lock:
            current, done, total = self._progress
        return replace(status, current=current, done=done, total=total)


class ImportJobs:
    """Slots and threads of the running steps, keyed by (owner, import id)."""

    def __init__(self) -> None:
        self._jobs: dict[tuple[str, str], ImportJob] = {}
        self._lock = threading.Lock()

    def reserve(self, owner: str, import_id: str, phase: str) -> ImportJob:
        """Take the user's slot; :class:`ImportConflict` when it or the server is full."""
        job = ImportJob(owner, import_id, phase)
        with self._lock:
            if (owner, import_id) in self._jobs:
                raise ImportConflict("running")
            mine = sum(1 for j in self._jobs.values() if j.owner == owner)
            if mine >= limits.MAX_RUNNING_PER_USER:
                raise ImportConflict("busy_user")
            if len(self._jobs) >= limits.MAX_RUNNING_TOTAL:
                raise ImportConflict("busy_server")
            self._jobs[(owner, import_id)] = job
        return job

    def release(self, job: ImportJob) -> None:
        with self._lock:
            if self._jobs.get((job.owner, job.import_id)) is job:
                del self._jobs[(job.owner, job.import_id)]

    def launch(self, job: ImportJob, run: Callable[[], None]) -> None:
        """Start ``run`` in a daemon thread; the slot is released when it returns."""

        def target() -> None:
            try:
                run()
            finally:
                self.release(job)

        job.thread = threading.Thread(target=target, name=f"import-{job.run_id[:8]}", daemon=True)
        try:
            job.thread.start()
        except RuntimeError as exc:
            log.warning("import step %s could not start a thread", job.run_id)
            self.release(job)
            raise ImportFailure() from exc

    def get(self, owner: str, import_id: str) -> ImportJob | None:
        with self._lock:
            return self._jobs.get((owner, import_id))

    def is_active(self, owner: str, import_id: str) -> bool:
        return self.get(owner, import_id) is not None

    def wait(self, owner: str, import_id: str, timeout: float) -> bool:
        job = self.get(owner, import_id)
        if job is not None and job.thread is not None:
            job.thread.join(timeout)
        return not self.is_active(owner, import_id)

    def close(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel.set()
        for job in jobs:
            if job.thread is not None:
                job.thread.join(JOIN_SECONDS)


# -- the analyse step -------------------------------------------------------------------------
@dataclass(frozen=True)
class AnalyseEnv:
    """Everything one analyse needs (plain values and callables; no request objects)."""

    store: ImportStore
    docx_store: Any
    collab: Collaborators
    owner: str
    import_id: str
    file_id: str
    file_name: str
    use_llm: bool
    settings: Any
    llm_factory: Callable[[Any], Any]
    open_client: OpenClient


def _with_issue(report: ImportedReport, code: str) -> ImportedReport:
    if any(i.code == code for i in report.issues):
        return report
    return replace(report, issues=(*report.issues, CompletenessIssue(code)))


def _close(resource: Any) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        try:
            close()
        except Exception as exc:  # noqa: BLE001 - cleanup must never mask the outcome
            log.debug("closing the LLM client failed (%s)", type(exc).__name__)


def _fill(env: AnalyseEnv, job: ImportJob, report: ImportedReport) -> ImportedReport:
    """The LLM fallback for what the reader left blank (only when asked for and configured)."""
    if not env.use_llm:
        return report
    if not getattr(env.settings, "llm_configured", False):
        return _with_issue(report, "llm_unavailable")
    llm = env.llm_factory(env.settings)
    try:
        outcome = env.collab.fill_gaps(
            report, llm=llm, settings=env.settings, cancelled=job.cancelled
        )
    finally:
        _close(llm)
    filled = outcome.report
    return _with_issue(filled, "llm_failed") if outcome.failed else filled


def _usable_blobs(plan: ImportPlan, blobs: Sequence[ImageBlob]) -> list[ImageBlob]:
    usable = {i.key for i in plan.images if i.usable}
    cap = limits.MAX_IMAGE_BYTES
    return [b for b in blobs if b.key in usable and 0 < len(b.data) <= cap]


def _read_source(env: AnalyseEnv) -> bytes:
    try:
        return env.docx_store.read_file(env.owner, env.file_id, "original")
    except DocxNotFound as exc:
        raise ImportNotFound("file") from exc


def analyse(env: AnalyseEnv, job: ImportJob) -> dict[str, Any]:
    """Reader, PwnDoc lookups, fallback, completeness; stores the plan and the images."""
    job.progress(READING, 0, ANALYSE_STEPS)
    data = _read_source(env)
    digest = sha256(data).hexdigest()
    report = env.collab.read_report(data, file_name=env.file_name)
    job.check()
    job.progress(CHECKING, 1, ANALYSE_STEPS)
    with env.open_client() as client:
        options, defaults = env.collab.load_options(client)
    job.check()
    job.progress(FILLING, 2, ANALYSE_STEPS)
    report = _fill(env, job, report)
    job.check()
    job.progress(COMPLETENESS, 3, ANALYSE_STEPS)
    audit_name = env.collab.default_audit_name(env.file_name)
    with env.open_client() as client:
        names = env.collab.check_names(client, report.header.company, audit_name)
    previous = env.store.find_created_by_sha(env.owner, digest, exclude=env.import_id)
    built = env.collab.build_plan(
        report,
        import_id=env.import_id,
        file_name=env.file_name,
        file_sha256=digest,
        options=options,
        defaults=defaults,
        names=names,
        previous_import=previous,
    )
    plan = ImportPlan.from_json(built.to_json())  # the strict checks of the stored form
    job.check()
    env.store.save_images(env.owner, env.import_id, _usable_blobs(plan, report.blobs))
    env.store.save_plan(env.owner, plan)
    job.progress(COMPLETENESS, ANALYSE_STEPS, ANALYSE_STEPS)
    return {"state": "ready", "revision": plan.revision, "has_preview": True}


# -- the create and undo steps ----------------------------------------------------------------
@dataclass(frozen=True)
class WriteEnv:
    store: ImportStore
    collab: Collaborators
    owner: str
    import_id: str
    open_client: OpenClient
    confirm_duplicate: bool = False


def create(env: WriteEnv, job: ImportJob, plan: ImportPlan) -> dict[str, Any]:
    """Run the writer; its rollback (on any failure after the first write) is kept in ``undo``."""
    journal = env.collab.journal_writer(
        env.store.journal_path(env.owner, env.import_id), env.import_id
    )
    outcome = env.collab.create_import(
        plan,
        open_client=env.open_client,
        read_image=lambda key: env.store.read_image(env.owner, env.import_id, key),
        journal=journal,
        progress=job.progress,
        cancelled=job.cancelled,
        confirm_duplicate=env.confirm_duplicate,
    )
    if outcome.error is None and outcome.result is not None:
        return {"state": "created", "result": outcome.result, "undo": None, "error": ""}
    error = outcome.error if isinstance(outcome.error, ImportFailure) else ImportFailure()
    return {"state": "create_failed", "error": str(error), "undo": outcome.rollback}


def undo(env: WriteEnv, job: ImportJob) -> dict[str, Any]:
    """Delete exactly the journaled ids; the journal records whether everything went."""
    path = env.store.journal_path(env.owner, env.import_id)
    journal = env.collab.read_journal(path)
    if journal is None or not journal.objects:
        raise ImportConflict("nothing_to_undo")
    result = env.collab.undo_import(journal, open_client=env.open_client, progress=job.progress)
    complete = all(item.outcome != "failed" for item in result.items)
    state = "undone" if complete else "partial"
    env.collab.write_journal(path, replace(journal, state=state, updated=utc_stamp()))
    if complete:
        return {"state": "undone", "undo": result, "error": ""}
    return {"state": "undo_failed", "undo": result, "error": str(ImportUpstreamError("pwndoc"))}


__all__ = [
    "ANALYSE_STEPS", "AnalyseEnv", "CHECKING", "COMPLETENESS", "Collaborators", "FILLING",
    "ImportJob", "ImportJobs", "OpenClient", "READING", "WriteEnv", "analyse", "create",
    "default_collaborators", "undo",
]  # fmt: skip
