"""Background runner of the Export tab analyses (convert a PDF, then detect the sections).

At most ``MAX_RUNNING`` analyses run at once across the server and ``MAX_PER_USER`` per user
(docs/EXPORT_DESIGN.md (a)). Each analysis runs in its own daemon thread; the work function gets
a cancel event and a progress callback. Progress lives in memory only: the record on disk holds
the state, so a restarted gateway finds an interrupted analysis as ``error``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .export_errors import ExportConflict

log = logging.getLogger(__name__)

MAX_RUNNING = 2
MAX_PER_USER = 1
STEPS = ("converting", "analysing", "")

Work = Callable[["JobHandle"], None]


@dataclass
class JobHandle:
    """What the work function sees: who, which file, the cancel flag and the progress setter."""

    owner: str
    file_id: str
    cancel: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    step: str = ""
    done: int = 0
    total: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def set_step(self, step: str) -> None:
        with self._lock:
            self.step = step if step in STEPS else ""
            self.done = self.total = 0

    def progress(self, done: int, total: int) -> None:
        with self._lock:
            self.total = max(0, int(total))
            self.done = max(0, min(int(done), self.total))

    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"step": self.step, "done": self.done, "total": self.total}


class ExportJobs:
    """Starts, tracks and cancels analysis threads (bounded server-wide and per user)."""

    def __init__(self, *, max_running: int = MAX_RUNNING, max_per_user: int = MAX_PER_USER):
        self._max_running = max_running
        self._max_per_user = max_per_user
        self._lock = threading.Lock()
        self._jobs: dict[tuple[str, str], JobHandle] = {}
        self._closed = False

    def _active(self) -> list[JobHandle]:
        return [job for job in self._jobs.values() if not job.finished.is_set()]

    def start(self, owner: str, file_id: str, work: Work) -> JobHandle:
        """Run ``work(handle)`` in a thread; :class:`ExportConflict` when busy."""
        with self._lock:
            if self._closed:
                raise ExportConflict("busy_server")
            active = self._active()
            if any(job.owner == owner and job.file_id == file_id for job in active):
                raise ExportConflict("running")
            if sum(1 for job in active if job.owner == owner) >= self._max_per_user:
                raise ExportConflict("busy_user")
            if len(active) >= self._max_running:
                raise ExportConflict("busy_server")
            handle = JobHandle(owner, file_id)
            self._jobs = {key: job for key, job in self._jobs.items() if not job.finished.is_set()}
            self._jobs[(owner, file_id)] = handle
        thread = threading.Thread(
            target=self._run, args=(work, handle), name="export-analysis", daemon=True
        )
        thread.start()
        return handle

    @staticmethod
    def _run(work: Work, handle: JobHandle) -> None:
        try:
            work(handle)
        except Exception as exc:  # noqa: BLE001 - the work function records its own errors
            log.warning("export analysis stopped (%s)", type(exc).__name__)
        finally:
            handle.finished.set()

    def get(self, owner: str, file_id: str) -> JobHandle | None:
        with self._lock:
            return self._jobs.get((owner, file_id))

    def is_active(self, owner: str, file_id: str) -> bool:
        job = self.get(owner, file_id)
        return job is not None and not job.finished.is_set()

    def cancel(self, owner: str, file_id: str) -> bool:
        job = self.get(owner, file_id)
        if job is None or job.finished.is_set():
            return False
        job.cancel.set()
        return True

    def wait(self, owner: str, file_id: str, timeout: float) -> bool:
        """True when the analysis of ``file_id`` has finished (or never ran) within ``timeout``."""
        job = self.get(owner, file_id)
        return True if job is None else job.finished.wait(timeout)

    def close(self) -> None:
        """Ask every running analysis to stop (gateway shutdown)."""
        with self._lock:
            self._closed = True
            jobs = self._active()
        for job in jobs:
            job.cancel.set()


__all__ = ["MAX_PER_USER", "MAX_RUNNING", "ExportJobs", "JobHandle"]
