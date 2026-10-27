"""The indexing job behind the "Index new files" button: one background thread, one run at a time.

The run indexes stored documents one after the other through :meth:`DocumentIndex.index_file`,
which is incremental (a file that is already indexed costs no embedding), so running it again is
cheap and nothing is ever embedded twice. Progress is polled with :meth:`RagIndexer.status`, which
returns a fresh copy every time. A run can be cancelled; the cancel is honoured between files and
between embedding batches, and what a half-indexed file already stored is picked up next time.

A document that cannot be read is reported as ``failed`` and the run goes on. A broken embedder
(``EmbedderError``) ends the run with state ``error``: every further file would fail the same way.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from .embedder import EmbedderError
from .rag import FAILED_MESSAGE, SUPPORTED, FileResult, IndexCancelled

log = logging.getLogger(__name__)

MAX_NAMES = 5000
MAX_NAME_CHARS = 255
MAX_ERROR_CHARS = 200
UNAVAILABLE_MESSAGE = "the local document index is unavailable"
CRASH_MESSAGE = "indexing stopped unexpectedly; see the gateway log"


class IndexerBusy(RuntimeError):
    """A run is already in progress."""


class _Index(Protocol):
    def index_file(
        self, path: Path, should_stop: Callable[[], bool] | None = None
    ) -> FileResult: ...


@dataclass(frozen=True)
class _Item:
    name: str
    status: str = "pending"
    chunks_new: int = 0
    chunks_reused: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "chunks_new": self.chunks_new,
            "chunks_reused": self.chunks_reused,
            "error": self.error,
        }


@dataclass(frozen=True)
class _Run:
    """The whole state of the job; replaced, never changed, so a snapshot can be taken anytime."""

    state: str = "idle"  # idle, running, done, error, cancelled
    items: tuple[_Item, ...] = ()
    current: str | None = None
    started: float | None = None
    ended: float | None = None
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "total": len(self.items),
            "done": sum(1 for item in self.items if item.status != "pending"),
            "current": self.current,
            "items": [item.as_dict() for item in self.items],
            "started": self.started,
            "ended": self.ended,
            "error": self.error,
        }


def _short(message: str) -> str:
    text = " ".join(message.split())
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 3] + "..."


class RagIndexer:
    """Indexes the stored documents in the background, one run at a time."""

    def __init__(
        self,
        index_factory: Callable[[], _Index | None] | _Index,
        docs_dir: str | Path,
        *,
        clock: Callable[[], float] = time.time,
    ):
        """``index_factory`` is called when a run starts (opening the index is heavy and may
        fail); an index object can be passed instead of a factory."""
        self._factory = index_factory
        self.docs_dir = Path(docs_dir)
        self._clock = clock
        self._lock = threading.Lock()
        self._run = _Run()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    # -- public ---------------------------------------------------------------------------
    def start(self, names: Sequence[str] | None = None) -> None:
        """Start a run over ``names`` (default: every stored document) in the background.

        Raises :class:`IndexerBusy` while a run is going and ``ValueError`` for a name that is
        not a stored document.
        """
        chosen = self._select(names)
        with self._lock:
            if self._run.state == "running":
                raise IndexerBusy("indexing is already running")
            cancel = threading.Event()  # a new event: a cancel of the last run cannot leak in
            self._cancel = cancel
            self._run = _Run("running", tuple(_Item(n) for n in chosen), started=self._clock())
            thread = threading.Thread(
                target=self._work, args=(chosen, cancel), name="rag-indexer", daemon=True
            )
            self._thread = thread
        thread.start()

    def status(self) -> dict[str, Any]:
        """A snapshot of the job; a new dict every time, so callers may keep or change it."""
        with self._lock:
            run = self._run
        return run.as_dict()

    def cancel(self) -> None:
        """Ask a running job to stop at the next file or embedding batch (no-op otherwise)."""
        with self._lock:
            if self._run.state == "running":
                self._cancel.set()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the running job to end; ``True`` when none is running any more."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def close(self) -> None:
        """Cancel a running job and wait for it (for a clean gateway shutdown)."""
        self.cancel()
        self.wait(30)

    # -- choosing the files ---------------------------------------------------------------
    def _select(self, names: Sequence[str] | None) -> list[str]:
        if names is None:
            return self._all_names()
        if not isinstance(names, list | tuple) or len(names) > MAX_NAMES:
            raise ValueError("names must be a list of stored documents")
        chosen = list(dict.fromkeys(names))  # no repeats, order kept
        for name in chosen:
            self._check_name(name)
        return chosen

    def _all_names(self) -> list[str]:
        try:
            entries = sorted(self.docs_dir.iterdir())
        except OSError:
            return []
        return [p.name for p in entries if self._is_document(p)]

    @staticmethod
    def _is_document(path: Path) -> bool:
        return path.suffix.lower() in SUPPORTED and path.is_file() and not path.is_symlink()

    def _check_name(self, name: object) -> None:
        if not isinstance(name, str) or not name or len(name) > MAX_NAME_CHARS:
            raise ValueError("invalid document name")
        if name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
            raise ValueError("invalid document name")
        if not self._is_document(self.docs_dir / name):
            raise ValueError("no such stored document")

    # -- the job --------------------------------------------------------------------------
    def _work(self, names: list[str], cancel: threading.Event) -> None:
        try:
            self._process(names, cancel)
        except Exception:  # noqa: BLE001 - the thread must always end in a final state
            log.exception("the indexing job crashed")
            self._finish("error", CRASH_MESSAGE)

    def _process(self, names: list[str], cancel: threading.Event) -> None:
        index = self._open_index()
        if index is None:
            return
        for position, name in enumerate(names):
            if cancel.is_set():
                return self._finish("cancelled")
            self._update(current=name)
            try:
                result = index.index_file(self.docs_dir / name, should_stop=cancel.is_set)
            except IndexCancelled:
                return self._finish("cancelled")
            except EmbedderError as exc:
                log.warning("indexing stopped: the embedder is unavailable")
                return self._finish("error", _short(str(exc)))
            except Exception as exc:  # noqa: BLE001 - one bad file never stops the run
                log.warning("could not index %s (%s)", name, type(exc).__name__)
                result = FileResult(name, "failed", error=FAILED_MESSAGE)
            self._record(position, result)
        self._finish("done")

    def _open_index(self) -> _Index | None:
        try:
            factory = self._factory
            index = (
                factory() if callable(factory) and not hasattr(factory, "index_file") else factory
            )
        except Exception:  # noqa: BLE001 - heavy start-up: report it, do not crash the thread
            log.exception("could not open the document index")
            self._finish("error", UNAVAILABLE_MESSAGE)
            return None
        if index is None:
            self._finish("error", UNAVAILABLE_MESSAGE)
        return index  # type: ignore[return-value]

    def _update(self, **changes: Any) -> None:
        with self._lock:
            self._run = replace(self._run, **changes)

    def _record(self, position: int, result: FileResult) -> None:
        with self._lock:
            name = self._run.items[position].name
            item = _Item(name, result.status, result.chunks_new, result.chunks_reused, result.error)
            items = self._run.items[:position] + (item,) + self._run.items[position + 1 :]
            self._run = replace(self._run, items=items, current=None)

    def _finish(self, state: str, error: str = "") -> None:
        self._update(state=state, current=None, ended=self._clock(), error=error)
