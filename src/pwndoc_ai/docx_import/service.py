"""The DOCX importer service: the state machine behind the ``/__ai/api/docx/import`` routes.

Every method takes the storage ``owner`` (``gw.docx_owner(identity)``): another owner's import
does not exist (:class:`ImportNotFound`). PwnDoc is reached only through ``open_client``, built
by the route from the user's own (newest) session cookie. Synchronous checks (state, revision,
blockers, duplicate audit) answer with an :class:`ImportFailure`; the analyse, create and undo
steps run in the background (:mod:`.jobs`) and report through the persisted status.

States (contract §c): ``analysing`` -> ``ready`` / ``analysis_failed`` / ``cancelled``;
``ready`` -> ``creating`` -> ``created`` / ``create_failed``; ``created`` (or a failed create or
undo that left objects) -> ``undoing`` -> ``undone`` / ``undo_failed``; any running state after a
restart -> ``interrupted``. A file creates in PwnDoc at most once per import.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from ..docx_jobs import EngineUnavailable
from ..docx_store import DocxNotFound
from ..pwndoc_client import PwnDocError
from . import limits
from .errors import (
    ImportCancelled,
    ImportConflict,
    ImportDenied,
    ImportEngineMissing,
    ImportFailure,
    ImportInputError,
    ImportNotFound,
    ImportSessionExpired,
    ImportUpstreamError,
)
from .jobs import (
    ANALYSE_STEPS,
    CHECKING,
    READING,
    AnalyseEnv,
    Collaborators,
    ImportJob,
    ImportJobs,
    OpenClient,
    WriteEnv,
    analyse,
    create,
    default_collaborators,
    undo,
)
from .model import AnalyseRequest, CreateRequest, ImportPlan, ImportStatus, PlanEdits
from .store import ImportStore, utc_stamp

log = logging.getLogger(__name__)

RUN_KIND = "docx_import"
CREATED_ONCE = frozenset({"created", "create_failed", "undoing", "undone", "undo_failed"})
UNDOABLE = frozenset({"created", "create_failed", "undo_failed", "interrupted"})
FINISHED_JOURNALS = frozenset({"rolled_back", "undone"})
FAILED_STATES = {"analyse": "analysis_failed", "create": "create_failed", "undo": "undo_failed"}
NAME_FIELDS = frozenset({"company", "audit_name"})


def pwndoc_failure(exc: PwnDocError) -> ImportFailure:
    """The user-safe error for a PwnDoc failure (the PwnDoc text goes to the log only)."""
    if exc.status == 401:
        return ImportSessionExpired()
    if exc.status == 403:
        return ImportDenied()
    log.warning("PwnDoc call of the importer failed (status %s)", exc.status)
    return ImportUpstreamError("pwndoc")


def user_failure(exc: BaseException) -> ImportFailure:
    """Any exception as a fixed-sentence :class:`ImportFailure` (logged by type only)."""
    if isinstance(exc, ImportFailure):
        return exc
    if isinstance(exc, PwnDocError):
        return pwndoc_failure(exc)
    if isinstance(exc, DocxNotFound):
        return ImportNotFound("file")
    log.warning("import step failed (%s)", type(exc).__name__)
    return ImportFailure()


def _file_name(name: str) -> str:
    cleaned = "".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in str(name or ""))
    return cleaned.strip()[: limits.MAX_FILE_NAME_CHARS]


class ImportService:
    def __init__(
        self,
        store: ImportStore,
        docx_store: Any,
        *,
        llm_factory: Callable[[Any], Any],
        settings: Callable[[], Any],
        runlog: Any = None,
        engine: Callable[[], Any] | None = None,
        collaborators: Collaborators | None = None,
    ):
        self._store = store
        self._docx_store = docx_store
        self._llm_factory = llm_factory
        self._settings = settings
        self._runlog = runlog
        self._engine = engine
        self.collab = collaborators or default_collaborators()
        self._jobs = ImportJobs()
        self._state_lock = threading.Lock()  # read-check-write of a plan or a state change
        store.mark_interrupted()

    # -- reading ----------------------------------------------------------------------------
    def get_status(self, owner: str, import_id: str) -> ImportStatus:
        status = self._store.status(owner, import_id)
        job = self._jobs.get(owner, import_id)
        return status if job is None else job.overlay(status)

    def list(self, owner: str) -> list[ImportStatus]:
        statuses = self._store.list(owner)
        return [self._live(owner, s) for s in statuses]

    def _live(self, owner: str, status: ImportStatus) -> ImportStatus:
        job = self._jobs.get(owner, status.id)
        return status if job is None else job.overlay(status)

    def preview(self, owner: str, import_id: str) -> ImportPlan:
        status = self._store.status(owner, import_id)
        if not status.has_preview:
            raise ImportConflict("not_ready")
        return self._store.read_plan(owner, import_id)

    def image(self, owner: str, import_id: str, key: str) -> tuple[bytes, str]:
        if not limits.IMAGE_KEY.fullmatch(key or ""):
            raise ImportNotFound("image")
        status = self._store.status(owner, import_id)
        if not status.has_preview:
            raise ImportNotFound("image")
        plan = self._store.read_plan(owner, import_id)
        image = next((i for i in plan.images if i.key == key), None)
        if image is None or not image.usable:
            raise ImportNotFound("image")
        return self._store.read_image(owner, import_id, key), image.media_type

    def wait(self, owner: str, import_id: str, timeout: float) -> bool:
        """Block until the import's running step is over (True) or ``timeout`` passes."""
        return self._jobs.wait(owner, import_id, timeout)

    # -- analyse ----------------------------------------------------------------------------
    def start_analysis(
        self, identity: Any, owner: str, req: AnalyseRequest, open_client: OpenClient
    ) -> ImportStatus:
        try:
            record = self._docx_store.get(owner, req.file_id)
        except DocxNotFound as exc:
            raise ImportNotFound("file") from exc
        self._check_engine()
        status = ImportStatus(
            id=uuid.uuid4().hex,
            file_id=req.file_id,
            file_name=_file_name(record.name),
            state="analysing",
            phase="analyse",
            total=ANALYSE_STEPS,
            current=READING,
            started=utc_stamp(),
        )
        job = self._jobs.reserve(owner, status.id, "analyse")
        try:
            self._store.create(owner, status)
        except BaseException:
            self._jobs.release(job)
            raise
        env = AnalyseEnv(
            store=self._store,
            docx_store=self._docx_store,
            collab=self.collab,
            owner=owner,
            import_id=status.id,
            file_id=req.file_id,
            file_name=status.file_name,
            use_llm=req.use_llm,
            settings=self._settings(),
            llm_factory=self._llm_factory,
            open_client=open_client,
        )
        self._launch(job, status, lambda: analyse(env, job))
        return status

    def _check_engine(self) -> None:
        if self._engine is None:
            return
        try:
            self._engine()
        except EngineUnavailable as exc:
            raise ImportEngineMissing() from exc

    # -- edit -------------------------------------------------------------------------------
    def _editable(self, owner: str, import_id: str) -> ImportStatus:
        status = self._store.status(owner, import_id)
        if self._jobs.is_active(owner, import_id):
            raise ImportConflict("running")
        if status.state != "ready" or not status.has_preview:
            raise ImportConflict("not_ready")
        return status

    def _names(self, plan: ImportPlan, edits: PlanEdits, open_client: OpenClient) -> Any:
        """The company and duplicate check for a header edit that names either; else None."""
        header = edits.header
        if header is None or not NAME_FIELDS & header.present:
            return None
        company = header.get("company", plan.header.company)
        audit_name = header.get("audit_name", plan.header.audit_name)
        return self._check_names(open_client, company, audit_name)

    def _check_names(self, open_client: OpenClient, company: str, audit_name: str) -> Any:
        try:
            with open_client() as client:
                return self.collab.check_names(client, company, audit_name)
        except PwnDocError as exc:
            raise pwndoc_failure(exc) from exc

    def edit(
        self, owner: str, import_id: str, edits: PlanEdits, open_client: OpenClient
    ) -> ImportPlan:
        self._editable(owner, import_id)
        plan = self._store.read_plan(owner, import_id)
        if edits.revision != plan.revision:
            raise ImportConflict("stale")
        names = self._names(plan, edits, open_client)
        with self._state_lock:
            status = self._editable(owner, import_id)
            current = self._store.read_plan(owner, import_id)
            changed = self.collab.apply_edits(current, edits, names=names)
            updated = ImportPlan.from_json(changed.to_json())
            self._store.save_plan(owner, updated)
            self._store.save_status(owner, replace(status, revision=updated.revision))
        return updated

    # -- create -----------------------------------------------------------------------------
    def _check_creatable(self, owner: str, status: ImportStatus) -> None:
        if self._jobs.is_active(owner, status.id):
            raise ImportConflict("running")
        if status.state in CREATED_ONCE:
            raise ImportConflict("already_created")
        if status.state == "interrupted" and self._store.has_journal(owner, status.id):
            raise ImportConflict("already_created")
        if status.state != "ready" or not status.has_preview:
            raise ImportConflict("not_ready")

    def _gate(self, plan: ImportPlan, req: CreateRequest) -> None:
        if req.revision != plan.revision:
            raise ImportConflict("stale")
        if self.collab.blockers(plan.issues):
            raise ImportInputError("blockers")
        if self.collab.warnings(plan.issues) and not req.accept_blanks:
            raise ImportInputError("blanks")

    def start_create(
        self,
        identity: Any,
        owner: str,
        import_id: str,
        req: CreateRequest,
        open_client: OpenClient,
    ) -> ImportStatus:
        self._check_creatable(owner, self._store.status(owner, import_id))
        plan = self._store.read_plan(owner, import_id)
        self._gate(plan, req)
        names = self._check_names(open_client, plan.header.company, plan.header.audit_name)
        if names.duplicate_audit and not req.confirm_duplicate:
            raise ImportConflict("duplicate_audit")
        with self._state_lock:
            status = self._store.status(owner, import_id)
            self._check_creatable(owner, status)
            plan = self._store.read_plan(owner, import_id)
            self._gate(plan, req)
            job, running = self._begin(owner, status, "create", "creating", CHECKING)
        env = WriteEnv(
            self._store, self.collab, owner, import_id, open_client, req.confirm_duplicate
        )
        self._launch(job, running, lambda: create(env, job, plan))
        return running

    def _begin(
        self, owner: str, status: ImportStatus, phase: str, state: str, current: str
    ) -> tuple[ImportJob, ImportStatus]:
        """Reserve the user's slot and persist the running status (caller holds the lock)."""
        job = self._jobs.reserve(owner, status.id, phase)
        running = replace(
            status,
            state=state,
            phase=phase,
            total=0,
            done=0,
            current=current,
            error="",
            started=utc_stamp(),
            ended="",
            undo=None,
        )
        try:
            self._store.save_status(owner, running)
        except BaseException:
            self._jobs.release(job)
            raise
        return job, running

    # -- cancel and undo --------------------------------------------------------------------
    def cancel(self, owner: str, import_id: str) -> ImportStatus:
        status = self._store.status(owner, import_id)
        job = self._jobs.get(owner, import_id)
        if job is None or job.phase == "undo":
            raise ImportConflict("not_ready")
        job.cancel.set()
        return job.overlay(status)

    def _undoable_journal(self, owner: str, status: ImportStatus) -> Any:
        if self._jobs.is_active(owner, status.id):
            raise ImportConflict("running")
        if status.state not in UNDOABLE:
            raise ImportConflict("nothing_to_undo")
        try:
            journal = self.collab.read_journal(self._store.journal_path(owner, status.id))
        except ImportInputError as exc:
            log.warning("the undo journal of an import is damaged")
            raise ImportFailure() from exc
        if journal is None or not journal.objects or journal.state in FINISHED_JOURNALS:
            raise ImportConflict("nothing_to_undo")
        return journal

    def start_undo(
        self, identity: Any, owner: str, import_id: str, open_client: OpenClient
    ) -> ImportStatus:
        with self._state_lock:
            status = self._store.status(owner, import_id)
            self._undoable_journal(owner, status)
            job, running = self._begin(owner, status, "undo", "undoing", "")
        env = WriteEnv(self._store, self.collab, owner, import_id, open_client)
        self._launch(job, running, lambda: undo(env, job))
        return running

    # -- delete and shutdown ----------------------------------------------------------------
    def delete(self, owner: str, import_id: str) -> None:
        """Remove the local preview, images and journal; PwnDoc is never touched."""
        with self._state_lock:
            if self._jobs.is_active(owner, import_id):
                raise ImportConflict("running")
            self._store.delete(owner, import_id)

    def close(self) -> None:
        self._jobs.close()

    # -- running a step ---------------------------------------------------------------------
    def _launch(self, job: ImportJob, status: ImportStatus, work: Callable[[], dict]) -> None:
        self._log(job, "start")
        try:
            self._jobs.launch(job, lambda: self._run(job, status, work))
        except ImportFailure as exc:
            self._finish(job, status, {"state": FAILED_STATES[job.phase], "error": str(exc)})
            raise

    def _run(self, job: ImportJob, status: ImportStatus, work: Callable[[], dict]) -> None:
        try:
            fields = work()
        except ImportCancelled as exc:
            cancelled = job.phase == "analyse"
            fields = {
                "state": "cancelled" if cancelled else FAILED_STATES[job.phase],
                "error": "" if cancelled else str(exc),
            }
        except Exception as exc:  # noqa: BLE001 - any failure must end the step visibly
            fields = {"state": FAILED_STATES[job.phase], "error": str(user_failure(exc))}
        self._finish(job, status, fields)

    def _finish(self, job: ImportJob, status: ImportStatus, fields: dict[str, Any]) -> None:
        """Persist the final status. Never raises (the import may have been deleted)."""
        final = replace(job.overlay(status), current="", ended=utc_stamp(), **fields)
        try:
            self._store.save_status(job.owner, final)
        except (ImportNotFound, OSError) as exc:
            log.warning(
                "import step %s: could not store its end (%s)", job.run_id, type(exc).__name__
            )
        self._log(job, "end", final)

    # -- run log (kind 'docx_import'; ids, phase, state and counts only) ----------------------
    def _log(self, job: ImportJob, event: str, final: ImportStatus | None = None) -> None:
        if self._runlog is None:
            return
        entry: dict[str, Any] = {
            "event": event,
            "job_id": job.run_id,
            "user": job.owner,
            "kind": RUN_KIND,
            "import_id": job.import_id,
            "phase": job.phase,
        }
        if final is not None:
            entry.update(_counts(final))
        self._runlog.record(entry)


def _counts(status: ImportStatus) -> dict[str, Any]:
    result, undone = status.result, status.undo
    return {
        "status": status.state,
        "findings": len(result.finding_ids) if result is not None else 0,
        "images_uploaded": result.images_uploaded if result is not None else 0,
        "undo_failed": undone.count("failed") if undone is not None else 0,
        "undo_kept": undone.count("kept") if undone is not None else 0,
    }


__all__ = ["ImportService", "pwndoc_failure", "user_failure"]
