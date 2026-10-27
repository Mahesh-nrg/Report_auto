"""Background refinement jobs: propose AI edits for findings, then apply them to PwnDoc.

One worker thread runs per job. Jobs are owned by the PwnDoc username that started them and live
in memory only (last ``MAX_JOBS`` kept). Proposals are never written to PwnDoc until applied, either
explicitly through :meth:`JobManager.apply` or immediately when ``auto_apply`` is set.

Applying is conflict-safe: the live finding is re-read from PwnDoc right before each write, the
first write saves a JSON backup of that *live* state, and a finding that someone edited since the
proposal was made is reported instead of being overwritten. Custom fields are merged onto the
live list by label so concurrent edits to other fields survive.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import notify as notify_module
from .config import Settings
from .history import prune_backups
from .job_support import (
    ACTIVE,
    APPLICABLE,
    AUDIT_BUSY,
    AUDIT_LOCKED,
    BACKUP_FAILED,
    CHANGED_SINCE,
    DEFAULT_LOCALE,
    FINAL_ITEM_STATES,
    PWNDOC_FAILED,
    SAFE_ID,
    SESSION_EXPIRED,
    UNSAVED,
    JobConflict,
    JobError,
    JobNotFound,
    SessionExpiredError,
    audit_language,
    changed_fields,
    clean_sources,
    id_of,
    user_message,
    write_payload,
)
from .jsonio import write_json_atomic
from .llm import Usage
from .masking import build_masker
from .notify import format_finish_message
from .reports import export_blocker, is_docx
from .stand_ins import prepare_corpus

if TYPE_CHECKING:
    from .history import RunLog

__all__ = [
    "CHANGED_SINCE", "SAFE_ID", "SESSION_EXPIRED", "Job", "JobConflict", "JobError", "JobItem",
    "JobManager", "JobNotFound", "SessionExpiredError", "changed_fields", "write_payload",
]  # fmt: skip

log = logging.getLogger(__name__)

MAX_JOBS = 50
MAX_RUNNING_JOBS = 4  # across all users: each job holds a thread and spends LLM budget

RefinerFactory = Callable[[], Any]
ClientFactory = Callable[[Mapping[str, str]], Any]
SettingsProvider = Callable[[], Settings]
# Cookies as a fixed mapping, or a callable returning the user's current cookies (so a refreshed
# PwnDoc session is picked up during a long job).
CookieSource = Mapping[str, str] | Callable[[], Mapping[str, str]]


class _SessionExpired(Exception):  # noqa: N818 - internal control flow, never leaves this module
    """PwnDoc rejected the user's token (401) while applying."""


def notify(url: str, text: str) -> bool:
    """Post to the webhook; looked up late so tests can patch it here or in ``notify``."""
    return notify_module.notify(url, text)


def _close(resource: Any) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - cleanup must never mask the job outcome
            log.debug("closing %r failed", resource, exc_info=True)


def _resolve(source: CookieSource) -> dict[str, str]:
    return dict(source() if callable(source) else source)


@dataclass(frozen=True)
class JobItem:
    finding_id: str
    title: str
    status: str = "pending"  # pending|running|proposed|applied|failed|skipped
    error: str = ""
    changes: Mapping[str, Any] = field(default_factory=dict)
    model: str = ""  # the model that answered ('' while unknown)
    sources: tuple[str, ...] = ()  # research citations (web hosts, document names)

    def view(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "finding_id": self.finding_id,
            "title": self.title,
            "status": self.status,
            "changes": dict(self.changes),
            "model": self.model,
            "sources": list(self.sources),
        }
        if self.error:
            data["error"] = self.error
        return data


class Job:
    def __init__(
        self,
        owner: str,
        audit_id: str,
        options: Any,
        auto_apply: bool,
        export_report: bool = False,
    ):
        self.id = uuid.uuid4().hex
        self.owner = owner
        self.audit_id = audit_id
        self.options = options
        self.auto_apply = auto_apply
        self.export_report = export_report
        self._report_ready = False  # the audit's Word report was built successfully at the end
        self.audit_name = ""
        self.start_logged = (
            threading.Event()
        )  # set once PwnDoc was read and the run log has 'start'
        self.end_logged = threading.Event()  # set once the job is over (and logged if it started)
        self.cancel_event = threading.Event()
        self.apply_lock = threading.Lock()
        self.backup_done = False
        self.session_expired = threading.Event()
        self.thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._status = "queued"
        self._error = ""
        self._usage = Usage()
        self._items: tuple[JobItem, ...] = ()
        self._proposals: dict[str, Any] = {}
        self._originals: dict[str, dict[str, Any]] = {}
        self._skipped: tuple[str, ...] = ()
        self._models: tuple[str, ...] = ()

    @property
    def kind(self) -> str:
        return "finish" if self.export_report else "update"

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    def set_status(self, status: str, error: str = "") -> None:
        with self._lock:
            self._status = status
            self._error = error

    def begin(self, findings: list[dict[str, Any]]) -> None:
        with self._lock:
            self._items = tuple(JobItem(id_of(f), str(f.get("title", ""))) for f in findings)
            self._originals = {id_of(f): f for f in findings}
            self._status = "running"

    def update_item(self, finding_id: str, **changes: Any) -> None:
        with self._lock:
            self._items = tuple(
                replace(item, **changes) if item.finding_id == finding_id else item
                for item in self._items
            )

    def item(self, finding_id: str) -> JobItem | None:
        with self._lock:
            return next((i for i in self._items if i.finding_id == finding_id), None)

    def record_proposal(self, finding_id: str, proposal: Any) -> None:
        model = str(getattr(proposal, "model", "") or "")
        with self._lock:
            self._proposals[finding_id] = proposal
            self._usage = self._usage + proposal.usage
            if model and model not in self._models:
                self._models = (*self._models, model)
        self.update_item(
            finding_id,
            status="proposed",
            error="",
            changes=proposal.changes,
            model=model,
            sources=clean_sources(getattr(proposal, "sources", ())),
        )

    def proposal(self, finding_id: str) -> Any | None:
        with self._lock:
            return self._proposals.get(finding_id)

    def originals(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._originals.values())

    def original(self, finding_id: str) -> dict[str, Any]:
        with self._lock:
            return self._originals.get(finding_id, {})

    def unsaved(self) -> int:
        """Findings whose rewrite never reached PwnDoc (applied/failed/skipped are final)."""
        with self._lock:
            return sum(1 for i in self._items if i.status not in FINAL_ITEM_STATES)

    def item_ids(self) -> set[str]:
        with self._lock:
            return {item.finding_id for item in self._items}

    def set_skipped(self, finding_ids: list[str]) -> None:
        with self._lock:
            self._skipped = tuple(finding_ids)

    def set_report_ready(self, ready: bool) -> None:
        with self._lock:
            self._report_ready = ready

    def snapshot(self) -> dict[str, Any]:
        """The run-log view of the job right now (counts, models, tokens, report flag)."""
        with self._lock:
            return {
                "status": self._status,
                "error": self._error,
                "applied": sum(1 for i in self._items if i.status == "applied"),
                "failed": sum(1 for i in self._items if i.status == "failed"),
                "models": list(self._models),
                "tokens": self._usage.input_tokens + self._usage.output_tokens,
                "report": self._report_ready,
            }

    def view(self) -> dict[str, Any]:
        with self._lock:
            data: dict[str, Any] = {
                "id": self.id,
                "audit_id": self.audit_id,
                "status": self._status,
                "usage": {"input": self._usage.input_tokens, "output": self._usage.output_tokens},
                "items": [item.view() for item in self._items],
                "report": self._report_ready,
            }
            if self._error:
                data["error"] = self._error
            if self._skipped:
                data["skipped"] = list(self._skipped)
            return data


class JobManager:
    def __init__(
        self,
        data_dir: str | Path,
        refiner_factory: RefinerFactory,
        client_factory: ClientFactory,
        max_jobs: int = MAX_JOBS,
        max_running: int = MAX_RUNNING_JOBS,
        settings_provider: SettingsProvider | None = None,
        runlog: RunLog | None = None,
    ):
        self._data_dir = Path(data_dir)
        # Without a provider the stock defaults apply (masking on, library on, no webhook).
        self._settings_provider = settings_provider or Settings
        self._runlog = runlog
        self._refiner_factory = refiner_factory
        self._client_factory = client_factory
        self._max_jobs = max_jobs
        self._max_running = max_running
        self._jobs: dict[str, Job] = {}
        self._exclusive: set[str] = set()  # audits held by a writer that must not overlap a job
        self._lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------------------------
    def start(
        self,
        owner: str,
        cookies: CookieSource,
        audit_id: str,
        finding_ids: list[str] | None,
        options: Any,
        auto_apply: bool = False,
        export_report: bool = False,
    ) -> Job:
        if not SAFE_ID.fullmatch(audit_id) or any(
            not SAFE_ID.fullmatch(f) for f in finding_ids or []
        ):
            raise ValueError("invalid audit or finding id")
        job = Job(owner, audit_id, options, auto_apply, export_report)
        with self._lock:
            if audit_id in self._exclusive:
                raise JobConflict(AUDIT_LOCKED)
            active = [j for j in self._jobs.values() if j.status in ACTIVE]
            # a cancelled job is winding down; it must not block the user's next one
            if any(j.owner == owner and not j.cancel_event.is_set() for j in active):
                raise JobConflict("you already have a job running")
            if len(active) >= self._max_running:
                raise JobConflict("the server is busy with other jobs; try again shortly")
            self._jobs[job.id] = job
            self._prune()
        wanted = set(finding_ids) if finding_ids else None
        job.thread = threading.Thread(
            target=self._run,
            args=(job, cookies, wanted),
            name=f"job-{job.id[:8]}",
            daemon=True,
        )
        try:
            job.thread.start()
        except RuntimeError:  # no thread available: end the job instead of leaving it queued
            job.set_status("error", "the server could not start the job")
            self._finish(job, None)
            raise
        return job

    def get(self, job_id: str, owner: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.owner != owner:
            raise JobNotFound(job_id)
        return job

    def cancel(self, job: Job) -> None:
        job.cancel_event.set()

    def _busy(self, audit_id: str) -> bool:
        """Caller holds the lock."""
        return any(j.audit_id == audit_id and j.status in ACTIVE for j in self._jobs.values())

    def audit_busy(self, audit_id: str) -> bool:
        """A job that may still write to this audit is running (or winding down)."""
        with self._lock:
            return self._busy(audit_id)

    @contextmanager
    def exclusive(self, audit_id: str) -> Iterator[None]:
        """Hold the audit for a writer that must not overlap an AI job (a backup restore).

        Raises :class:`JobConflict` when a job is active on the audit or another writer holds
        it. While held, :meth:`start` refuses jobs for this audit.
        """
        with self._lock:
            if audit_id in self._exclusive or self._busy(audit_id):
                raise JobConflict(AUDIT_BUSY)
            self._exclusive.add(audit_id)
        try:
            yield
        finally:
            with self._lock:
                self._exclusive.discard(audit_id)

    def active_job_ids(self) -> set[str]:
        """Jobs that are running or whose end event is not in the run log yet.

        Pass this to ``RunLog.runs_for_audit(live_job_ids=...)`` so a job that is merely still
        winding down is not reported as ``interrupted``.
        """
        with self._lock:
            jobs = list(self._jobs.values())
        return {j.id for j in jobs if j.status in ACTIVE or not j.end_logged.is_set()}

    def _prune(self) -> None:
        while len(self._jobs) > self._max_jobs:
            finished = next((k for k, j in self._jobs.items() if j.status not in ACTIVE), None)
            if finished is None:
                return
            del self._jobs[finished]

    # -- worker -----------------------------------------------------------------------------
    def _client_for(self, source: CookieSource) -> Any:
        return self._client_factory(_resolve(source))

    def _run(self, job: Job, source: CookieSource, wanted: set[str] | None) -> None:
        client = refiner = settings = None
        try:
            settings = self._settings_provider()
            client = self._client_for(source)
            audit = self._audit_for(job, client, settings)
            if audit is not None:
                self._mark_started(job)
            job.audit_name = str(audit.get("name") or "") if audit is not None else ""
            if job.export_report and audit is not None and not self._preflight(job, audit):
                return
            findings = self._select(job, client, audit, wanted)
            self._mark_started(job)  # the first successful read: the user may see this audit
            if not findings:
                job.set_status("error", "no matching findings in this audit")
                return
            job.begin(findings)
            refiner = self._refiner_factory()
            self._prepare_refiner(job, refiner, client, audit, settings, findings)
            for finding in findings:
                if job.cancel_event.is_set():
                    break
                self._process(job, refiner, client, source, finding)
            self._conclude(job, source)
        except Exception as exc:  # noqa: BLE001 - any failure must end the job visibly
            log.exception("job %s failed", job.id)
            job.set_status("error", user_message(exc))
        finally:
            _close(refiner)
            _close(client)
            self._finish(job, settings)

    @staticmethod
    def _audit_for(job: Job, client: Any, settings: Settings) -> Mapping[str, Any] | None:
        """The audit document, when the job needs it (report preflight, masker or library language).

        Read once and reused for the findings, so these cost no extra request. Clients without
        ``get_audit`` (test fakes) fall back to ``list_findings`` as before.
        """
        wanted = settings.mask_sensitive or settings.use_library
        optional = wanted and callable(getattr(client, "get_audit", None))
        if not (job.export_report or optional):
            return None
        audit = client.get_audit(job.audit_id)
        return audit if isinstance(audit, Mapping) else None

    def _prepare_refiner(
        self,
        job: Job,
        refiner: Any,
        client: Any,
        audit: Mapping[str, Any] | None,
        settings: Settings,
        findings: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """Give the (per-job) refiner this job's masker and the library, when it supports them.

        Every finding of the job is registered with the masker before the first one is masked,
        so no stand-in can equal text that appears anywhere in this job's findings.
        """
        if callable(getattr(refiner, "use_masker", None)):
            # a masker that fails to build or prepare fails the job: unmasked text would be worse
            masker = build_masker(audit or {}, settings.mask_sensitive)
            prepare_corpus(masker, list(findings))
            refiner.use_masker(masker)
        if settings.use_library and callable(getattr(refiner, "use_library", None)):
            locale = audit_language(audit)
            items = self._library_items(job, client, locale)
            if items is not None:
                refiner.use_library(items, locale)

    @staticmethod
    def _library_items(
        job: Job, client: Any, locale: str = DEFAULT_LOCALE
    ) -> list[Mapping[str, Any]] | None:
        """The library wording in the audit's language, fetched once per job.

        PwnDoc's plain list endpoint returns titles only, so the per-language one is used.
        Clients without it (test fakes) fall back to the plain list. Any failure just skips
        the hint.
        """
        fetch = getattr(client, "list_vulnerabilities_by_locale", None)
        if callable(fetch):
            fetch = partial(fetch, locale)
        else:
            fetch = getattr(client, "list_vulnerabilities", None)
        if not callable(fetch):
            return None
        try:
            items = fetch()
        except Exception as exc:  # noqa: BLE001 - the library is optional context
            log.warning("job %s: library not available (%s)", job.id, exc)
            return None
        if not isinstance(items, list):
            log.warning("job %s: library answer ignored (not a list)", job.id)
            return None
        return [item for item in items if isinstance(item, Mapping)]

    # -- run log and notification ---------------------------------------------------------------
    def _mark_started(self, job: Job) -> None:
        """Write the 'start' event once, after PwnDoc answered the first read for the user.

        Before that nothing proves the user may see the audit, so a request for an audit they
        cannot open leaves no trace in the run log.
        """
        if not job.start_logged.is_set():
            job.start_logged.set()
            self._log(job, "start")

    def _log(self, job: Job, name: str, **extra: Any) -> None:
        if self._runlog is None:
            return
        event = {
            "event": name,
            "job_id": job.id,
            "audit_id": job.audit_id,
            "user": job.owner,
            "kind": job.kind,
            **extra,
        }
        try:
            self._runlog.record(event)
        except Exception:  # noqa: BLE001 - history must never break a job
            log.warning("job %s: could not write the run log", job.id, exc_info=True)

    def _finish(self, job: Job, settings: Settings | None) -> None:
        """Record the end event exactly once, then notify (finish jobs only). Never raises."""
        if job.end_logged.is_set():
            return
        snap: dict[str, Any] = {}
        try:
            if job.status in ACTIVE:  # a worker that died mid-way must not look unfinished
                job.set_status("error", "the job ended unexpectedly")
            snap = job.snapshot()
            if job.start_logged.is_set():  # an 'end' without a 'start' would be an orphan
                self._log(job, "end", **snap)
        except Exception:  # noqa: BLE001
            log.exception("job %s: could not record the end of the job", job.id)
        finally:
            job.end_logged.set()
        if snap:
            self._notify(job, settings, snap)

    @staticmethod
    def _notify(job: Job, settings: Settings | None, snap: Mapping[str, Any]) -> None:
        url = settings.notify_webhook_url if settings is not None else ""
        if job.kind != "finish" or not url:
            return
        try:
            text = format_finish_message(
                audit_name=job.audit_name,
                user=job.owner,
                status=snap["status"],
                applied=snap["applied"],
                failed=snap["failed"],
                report=snap["report"],
                error=snap["error"],
            )
            notify(url, text)
        except Exception as exc:  # noqa: BLE001 - a dead webhook must not affect the job
            # type only: the message may quote the webhook URL, which carries a secret token
            log.warning("job %s: notification failed (%s)", job.id, type(exc).__name__)

    @staticmethod
    def _preflight(job: Job, audit: Mapping[str, Any]) -> bool:
        """Refuse before any AI work when the Word export could not succeed anyway."""
        blocker = export_blocker(audit)
        if blocker:
            job.set_status("error", blocker)
            return False
        return True

    @staticmethod
    def _select(
        job: Job, client: Any, audit: Mapping[str, Any] | None, wanted: set[str] | None
    ) -> list[dict[str, Any]]:
        raw = (
            (audit.get("findings") or [])
            if audit is not None
            else client.list_findings(job.audit_id)
        )
        return [f for f in raw if id_of(f) and (wanted is None or id_of(f) in wanted)]

    def _conclude(self, job: Job, source: CookieSource) -> None:
        """Export the report (when asked) and set the final status."""
        cancelled = job.cancel_event.is_set()
        if job.export_report and not cancelled:
            lost = job.unsaved()
            if lost:  # never hand over a report that silently lacks the rewrite
                job.set_status("error", UNSAVED.format(n=lost, s=" was" if lost == 1 else "s were"))
                return
            if not self._verify_report(job, source):
                return
            if job.cancel_event.is_set():  # cancelled while the report was being built
                job.set_report_ready(False)
                cancelled = True
        job.set_status("cancelled" if cancelled else "done")

    def _process(
        self, job: Job, refiner: Any, client: Any, source: CookieSource, finding: dict[str, Any]
    ) -> None:
        fid = id_of(finding)
        job.update_item(fid, status="running")
        try:
            job.record_proposal(fid, refiner.refine(finding, job.options))
        except Exception as exc:  # noqa: BLE001 - one bad finding must not stop the batch
            log.warning("refine failed for %s: %s", fid, exc)
            job.update_item(fid, status="failed", error=user_message(exc))
            return
        if not job.auto_apply or job.cancel_event.is_set():  # cancelled mid-call: leave it proposed
            return
        if job.session_expired.is_set():
            job.update_item(fid, error=SESSION_EXPIRED)
            return
        fresh = self._client_for(source) if callable(source) else client
        try:
            with job.apply_lock:
                if job.cancel_event.is_set():
                    return
                self._apply_one(job, fresh, fid)
            return
        except _SessionExpired:
            if not callable(source):  # a fixed cookie cannot get any fresher
                job.session_expired.set()
                return
        finally:
            if fresh is not client:
                _close(fresh)
        self._retry_apply(job, source, fid)

    def _retry_apply(self, job: Job, source: CookieSource, finding_id: str) -> None:
        """PwnDoc rotates tokens while a run is going; try once more with the newest one."""
        newest = self._client_for(source)
        try:
            with job.apply_lock:
                self._apply_one(job, newest, finding_id)
        except _SessionExpired:
            job.session_expired.set()
        finally:
            _close(newest)

    def _verify_report(self, job: Job, source: CookieSource) -> bool:
        """Build the Word report once with the user's *current* session to prove it works.

        Nothing is stored: the report is always rebuilt on demand from the audit in PwnDoc.
        """
        client = self._client_for(source)
        try:
            data = client.export_docx(job.audit_id)
        except Exception as exc:  # noqa: BLE001 - any failure must end the job visibly
            status = getattr(exc, "status", None)
            log.warning("report export failed for audit %s: %s", job.audit_id, exc)
            if status == 401:
                reason = SESSION_EXPIRED
            else:
                reason = f"PwnDoc answered {status}" if status else "PwnDoc is unreachable"
            job.set_status("error", f"report export failed ({reason}); the findings are saved")
            return False
        finally:
            _close(client)
        if not is_docx(data):
            job.set_status("error", "report export failed (PwnDoc did not return a Word file)")
            return False
        job.set_report_ready(True)
        return True

    # -- apply ------------------------------------------------------------------------------
    def apply(self, job: Job, cookies: CookieSource, finding_ids: list[str]) -> None:
        unknown = [f for f in finding_ids if job.item(f) is None]
        if unknown:
            raise ValueError(f"not part of this job: {', '.join(unknown)}")
        client = self._client_for(cookies)
        skipped: tuple[str, ...] = ()
        try:
            with job.apply_lock:
                live = self._live_by_id(job, client)
                skipped = self._apply_each(job, client, finding_ids, live)
        finally:
            _close(client)
            job.set_skipped(list(skipped))
            counts = job.snapshot()
            self._log(job, "progress", applied=counts["applied"], failed=counts["failed"])

    def _apply_each(
        self, job: Job, client: Any, finding_ids: list[str], live: dict[str, dict[str, Any]]
    ) -> tuple[str, ...]:
        """Apply the listed proposals in order; the ids with nothing applicable are returned."""
        skipped: list[str] = []
        for position, fid in enumerate(finding_ids):
            try:
                if not self._apply_one(job, client, fid, live):
                    skipped.append(fid)
            except _SessionExpired:
                for rest in finding_ids[position + 1 :]:
                    if job.item(rest) is not None:
                        job.update_item(rest, error=SESSION_EXPIRED)
                break
        return tuple(skipped)

    def _live_by_id(self, job: Job, client: Any) -> dict[str, dict[str, Any]]:
        """Read the audit as it is now and (first time only) back it up before any write."""
        try:
            live = client.list_findings(job.audit_id)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) == 401:
                raise SessionExpiredError(SESSION_EXPIRED) from exc
            log.warning("job %s: could not read audit %s: %s", job.id, job.audit_id, exc)
            raise JobError(PWNDOC_FAILED) from exc
        self._ensure_backup(job, live)
        return {id_of(f): f for f in live if id_of(f)}

    def _ensure_backup(self, job: Job, live: list[dict[str, Any]]) -> None:
        if job.backup_done:
            return
        wanted = job.item_ids()
        snapshot = [f for f in live if id_of(f) in wanted] or job.originals()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = self._data_dir / "backups" / job.audit_id / f"{stamp}_{job.id[:8]}.json"
        try:
            write_json_atomic(path, snapshot)
        except OSError as exc:
            log.warning("job %s: could not write the backup: %s", job.id, exc)
            raise JobError(BACKUP_FAILED) from exc
        job.backup_done = True
        prune_backups(self._data_dir, job.audit_id)

    def _apply_one(
        self,
        job: Job,
        client: Any,
        finding_id: str,
        live_by_id: dict[str, dict[str, Any]] | None = None,
    ) -> bool:
        """Write one proposal. False if there was nothing applicable (so it counts as skipped)."""
        from .refiner import finding_update_payload

        item, proposal = job.item(finding_id), job.proposal(finding_id)
        if item is None or proposal is None or item.status not in APPLICABLE:
            return False
        if not proposal.payload:  # nothing to write; do not send an empty update
            job.update_item(finding_id, status="skipped", error="")
            return True
        try:
            if live_by_id is None:
                live_by_id = self._live_by_id(job, client)
            live = live_by_id.get(finding_id)
            if live is None:
                raise JobError("the finding no longer exists in PwnDoc")
            if changed_fields(job.original(finding_id), live, proposal):
                raise JobError(CHANGED_SINCE)
            payload = finding_update_payload(write_payload(live, proposal))
            client.update_finding(job.audit_id, finding_id, payload)
        except Exception as exc:  # noqa: BLE001 - report PwnDoc/backup errors per finding
            if getattr(exc, "status", None) == 401:
                job.update_item(finding_id, status="proposed", error=SESSION_EXPIRED)
                raise _SessionExpired from exc
            log.warning("apply failed for %s: %s", finding_id, exc)
            job.update_item(finding_id, status="failed", error=user_message(exc))
            return True
        job.update_item(finding_id, status="applied", error="")
        return True
