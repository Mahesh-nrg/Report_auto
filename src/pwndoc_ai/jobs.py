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
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .jsonio import write_json_atomic
from .llm import Usage

log = logging.getLogger(__name__)

MAX_JOBS = 50
MAX_RUNNING_JOBS = 4  # across all users: each job holds a thread and spends LLM budget
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ACTIVE = ("queued", "running")
APPLICABLE = ("proposed", "failed")

RefinerFactory = Callable[[], Any]
ClientFactory = Callable[[Mapping[str, str]], Any]
# Cookies as a fixed mapping, or a callable returning the user's current cookies (so a refreshed
# PwnDoc session is picked up during a long job).
CookieSource = Mapping[str, str] | Callable[[], Mapping[str, str]]

SESSION_EXPIRED = "session expired: sign in to PwnDoc again, then apply manually"
CHANGED_SINCE = "changed in PwnDoc since the proposal; re-run to refresh"
CUSTOM_PREFIX = "customFields: "


class JobError(RuntimeError):
    pass


class SessionExpiredError(JobError):
    """PwnDoc rejected the user's token; ``status`` lets callers treat it like a 401."""

    status = 401


class JobConflict(JobError):
    """The user already has a running job, or the server is at its job limit."""


class _SessionExpired(Exception):  # noqa: N818 - internal control flow, never leaves this module
    """PwnDoc rejected the user's token (401) while applying."""


class JobNotFound(KeyError):
    pass


def _finding_id(finding: Mapping[str, Any]) -> str:
    return str(finding.get("_id") or finding.get("id") or "")


def _custom_label(entry: Any) -> str:
    custom = entry.get("customField") if isinstance(entry, dict) else None
    if not isinstance(custom, dict):
        return ""
    return str(custom.get("label") or custom.get("display") or "").strip().lower()


def _changed_labels(proposal: Any) -> set[str]:
    return {
        key[len(CUSTOM_PREFIX) :].strip().lower()
        for key in proposal.changes
        if key.startswith(CUSTOM_PREFIX)
    }


def _custom_texts(finding: Mapping[str, Any], labels: set[str]) -> dict[str, Any]:
    return {
        label: entry.get("text")
        for entry in finding.get("customFields") or []
        if (label := _custom_label(entry)) in labels
    }


def _same(a: Any, b: Any) -> bool:
    return (a if a is not None else "") == (b if b is not None else "")


def changed_fields(
    original: Mapping[str, Any], live: Mapping[str, Any], proposal: Any
) -> list[str]:
    """Fields the proposal writes whose live value no longer matches the job-start snapshot."""
    stale = []
    for key in proposal.payload:
        if key == "customFields":
            labels = _changed_labels(proposal) or {_custom_label(e) for e in live.get(key) or []}
            if _custom_texts(original, labels) != _custom_texts(live, labels):
                stale.append(key)
        elif not _same(original.get(key), live.get(key)):
            stale.append(key)
    return stale


def write_payload(live: Mapping[str, Any], proposal: Any) -> dict[str, Any]:
    """The proposal's payload with custom fields merged onto the LIVE list by label."""
    payload = dict(proposal.payload)
    labels = _changed_labels(proposal)
    if "customFields" in payload and labels:
        proposed = {_custom_label(entry): entry.get("text") for entry in payload["customFields"]}
        payload["customFields"] = [
            {**entry, "text": proposed[label]}
            if (label := _custom_label(entry)) in labels and label in proposed
            else entry
            for entry in live.get("customFields") or []
        ]
    return payload


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

    def view(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "finding_id": self.finding_id,
            "title": self.title,
            "status": self.status,
            "changes": dict(self.changes),
        }
        if self.error:
            data["error"] = self.error
        return data


class Job:
    def __init__(self, owner: str, audit_id: str, options: Any, auto_apply: bool):
        self.id = uuid.uuid4().hex
        self.owner = owner
        self.audit_id = audit_id
        self.options = options
        self.auto_apply = auto_apply
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
            self._items = tuple(JobItem(_finding_id(f), str(f.get("title", ""))) for f in findings)
            self._originals = {_finding_id(f): f for f in findings}
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
        with self._lock:
            self._proposals[finding_id] = proposal
            self._usage = self._usage + proposal.usage
        self.update_item(finding_id, status="proposed", error="", changes=proposal.changes)

    def proposal(self, finding_id: str) -> Any | None:
        with self._lock:
            return self._proposals.get(finding_id)

    def originals(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._originals.values())

    def original(self, finding_id: str) -> dict[str, Any]:
        with self._lock:
            return self._originals.get(finding_id, {})

    def item_ids(self) -> set[str]:
        with self._lock:
            return {item.finding_id for item in self._items}

    def set_skipped(self, finding_ids: list[str]) -> None:
        with self._lock:
            self._skipped = tuple(finding_ids)

    def view(self) -> dict[str, Any]:
        with self._lock:
            data: dict[str, Any] = {
                "id": self.id,
                "audit_id": self.audit_id,
                "status": self._status,
                "usage": {"input": self._usage.input_tokens, "output": self._usage.output_tokens},
                "items": [item.view() for item in self._items],
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
    ):
        self._data_dir = Path(data_dir)
        self._refiner_factory = refiner_factory
        self._client_factory = client_factory
        self._max_jobs = max_jobs
        self._max_running = max_running
        self._jobs: dict[str, Job] = {}
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
    ) -> Job:
        if not SAFE_ID.match(audit_id) or any(not SAFE_ID.match(f) for f in finding_ids or []):
            raise ValueError("invalid audit or finding id")
        job = Job(owner, audit_id, options, auto_apply)
        with self._lock:
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
        job.thread.start()
        return job

    def get(self, job_id: str, owner: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.owner != owner:
            raise JobNotFound(job_id)
        return job

    def cancel(self, job: Job) -> None:
        job.cancel_event.set()

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
        client = refiner = None
        try:
            client = self._client_for(source)
            findings = [
                f
                for f in client.list_findings(job.audit_id)
                if _finding_id(f) and (wanted is None or _finding_id(f) in wanted)
            ]
            if not findings:
                job.set_status("error", "no matching findings in this audit")
                return
            job.begin(findings)
            refiner = self._refiner_factory()
            for finding in findings:
                if job.cancel_event.is_set():
                    break
                self._process(job, refiner, client, source, finding)
            job.set_status("cancelled" if job.cancel_event.is_set() else "done")
        except Exception as exc:  # noqa: BLE001 - any failure must end the job visibly
            log.exception("job %s failed", job.id)
            job.set_status("error", str(exc))
        finally:
            _close(refiner)
            _close(client)

    def _process(
        self, job: Job, refiner: Any, client: Any, source: CookieSource, finding: dict[str, Any]
    ) -> None:
        fid = _finding_id(finding)
        job.update_item(fid, status="running")
        try:
            job.record_proposal(fid, refiner.refine(finding, job.options))
        except Exception as exc:  # noqa: BLE001 - one bad finding must not stop the batch
            log.warning("refine failed for %s: %s", fid, exc)
            job.update_item(fid, status="failed", error=str(exc))
            return
        if not job.auto_apply:
            return
        if job.session_expired.is_set():
            job.update_item(fid, error=SESSION_EXPIRED)
            return
        fresh = self._client_for(source) if callable(source) else client
        try:
            with job.apply_lock:
                self._apply_one(job, fresh, fid)
        except _SessionExpired:
            job.session_expired.set()
        finally:
            if fresh is not client:
                _close(fresh)

    # -- apply ------------------------------------------------------------------------------
    def apply(self, job: Job, cookies: CookieSource, finding_ids: list[str]) -> None:
        unknown = [f for f in finding_ids if job.item(f) is None]
        if unknown:
            raise ValueError(f"not part of this job: {', '.join(unknown)}")
        client = self._client_for(cookies)
        skipped: list[str] = []
        try:
            with job.apply_lock:
                live = self._live_by_id(job, client)
                for position, fid in enumerate(finding_ids):
                    try:
                        if not self._apply_one(job, client, fid, live):
                            skipped.append(fid)
                    except _SessionExpired:
                        for rest in finding_ids[position + 1 :]:
                            if job.item(rest) is not None:
                                job.update_item(rest, error=SESSION_EXPIRED)
                        break
        finally:
            _close(client)
            job.set_skipped(skipped)

    def _live_by_id(self, job: Job, client: Any) -> dict[str, dict[str, Any]]:
        """Read the audit as it is now and (first time only) back it up before any write."""
        try:
            live = client.list_findings(job.audit_id)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) == 401:
                raise SessionExpiredError(SESSION_EXPIRED) from exc
            raise JobError(f"could not read the audit from PwnDoc: {exc}") from exc
        self._ensure_backup(job, live)
        return {_finding_id(f): f for f in live if _finding_id(f)}

    def _ensure_backup(self, job: Job, live: list[dict[str, Any]]) -> None:
        if job.backup_done:
            return
        wanted = job.item_ids()
        snapshot = [f for f in live if _finding_id(f) in wanted] or job.originals()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = self._data_dir / "backups" / job.audit_id / f"{stamp}_{job.id[:8]}.json"
        try:
            write_json_atomic(path, snapshot)
        except OSError as exc:
            raise JobError(f"could not write backup, nothing applied: {exc}") from exc
        job.backup_done = True

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
            job.update_item(finding_id, status="failed", error=str(exc))
            return True
        job.update_item(finding_id, status="applied", error="")
        return True
