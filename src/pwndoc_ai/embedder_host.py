"""The single embedder of a gateway process.

Everything that needs vectors (indexing local documents, searching them, ranking web results) uses
the one object this class hands out, so there is one pipeline and one on-demand Ollama server.
It is built lazily from the settings, rebuilt when the embedder settings change (never while an
indexing run is using it), closed at shutdown, and described for the admin UI by :meth:`status`.
``None`` stands for ChromaDB's built-in ONNX embedder (Ollama is off or not installed).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import ConfigStore, Settings
from .embedder import EmbedderError, is_external_url, make_embedder

log = logging.getLogger(__name__)

ONNX_MODEL = "all-MiniLM-L6-v2"
ONNX_SPACE = "minilm-l6-v2"
ONNX_CACHE = ("chroma", "onnx_models", ONNX_MODEL, "onnx", "model.onnx")  # under ~/.cache
IDLE, PREPARING, READY, FAILED = "idle", "preparing", "ready", "error"
PREPARING_DETAIL = "Preparing the embedder: the first run downloads the model."
FAILED_DETAIL = "The embedder could not be prepared; see the gateway log."
EXTERNAL_WARNING = (
    "The Ollama URL points outside your local network and masking of sensitive data is off: "
    "document text, search results and draft excerpts are sent to that server as they are."
)


def _settings_key(settings: Settings) -> tuple[str, str, int, str]:
    return (
        settings.embed_backend,
        settings.ollama_model,
        settings.ollama_idle_seconds,
        settings.ollama_url,
    )


class EmbedderHost:
    """Owns the process-wide embedder: lazy build, rebuild on new settings, status and prepare."""

    def __init__(self, store: ConfigStore, *, busy: Callable[[], bool] = lambda: False):
        self._store = store
        self._busy = busy  # True while an indexing run holds the current embedder
        self._lock = threading.RLock()
        self._built = False
        self._embedder: Any | None = None
        self._key: tuple[str, str, int, str] | None = None
        self._state = IDLE
        self._done: threading.Event | None = None  # set when the running prepare ends

    # -- life cycle -----------------------------------------------------------------------
    def get(self) -> Any | None:
        """The embedder for the current settings (``None``: the built-in ONNX one)."""
        settings = self._store.settings
        key = _settings_key(settings)
        with self._lock:
            if self._built and key != self._key and not self._busy():
                self._discard()
            if not self._built:
                self._embedder, self._key, self._built = self._build(settings), key, True
                self._state = IDLE
            return self._embedder

    def _build(self, settings: Settings) -> Any | None:
        try:
            embedder = make_embedder(self._store.data_dir, settings, self._store.env)
        except EmbedderError as exc:
            log.warning("the Ollama embedder is disabled: %s", exc)
            return None
        reap = getattr(embedder, "reap_orphan", None)
        if callable(reap):
            try:
                reap()  # a server a killed gateway left behind goes now, not at the next embed
            except Exception:  # noqa: BLE001 - cleaning up must never stop the embedder
                log.exception("could not look for Ollama servers left behind")
        return embedder

    def _discard(self) -> None:
        old, self._embedder, self._built = self._embedder, None, False
        if old is not None:
            self._close_quietly(old)

    @staticmethod
    def _close_quietly(embedder: Any) -> None:
        try:
            embedder.close()
        except Exception:  # noqa: BLE001 - shutting down or replacing: nothing else to do
            log.exception("could not stop the embedder")

    def close(self) -> None:
        """Stop the embedder server this process started (an external one is never touched)."""
        with self._lock:
            if self._embedder is not None:
                self._close_quietly(self._embedder)

    # -- status and prepare ---------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """What the admin UI shows: backend, model, vector space and whether it is ready."""
        embedder = self.get()
        wanted = self._store.settings.embed_backend
        with self._lock:
            state = self._state
        if embedder is None:
            return {**_onnx_status(wanted), "state": state}
        answer = {**_ollama_status(embedder, state), "state": state}
        if self._exposes_text():
            answer["warning"] = EXTERNAL_WARNING
        return answer

    def _exposes_text(self) -> bool:
        """An external server outside the LAN gets unmasked text when masking is off."""
        settings = self._store.settings
        return not settings.mask_sensitive and is_external_url(settings.ollama_url)

    def prepare(self, wait: float = 0.0) -> dict[str, Any]:
        """Start the embedder and download its model when missing, in the background.

        With ``wait`` the call also waits up to that many seconds for the outcome (a model already
        on disk is ready within a few of them); the status says ``preparing`` while it runs on.
        """
        embedder = self.get()
        done: threading.Event | None = None
        with self._lock:
            if embedder is not None:
                if self._state != PREPARING:
                    self._state, self._done = PREPARING, threading.Event()
                    threading.Thread(
                        target=self._run_prepare,
                        args=(embedder, self._done),
                        name="embedder-prepare",
                        daemon=True,
                    ).start()
                done = self._done
        if done is not None and wait > 0:
            done.wait(wait)
        return self.status()

    def _run_prepare(self, embedder: Any, done: threading.Event) -> None:
        outcome = READY
        try:
            embedder.prepare()  # starts the server and pulls the model when it is missing
        except EmbedderError as exc:
            log.warning("could not prepare the embedder: %s", exc)
            outcome = FAILED
        except Exception:  # noqa: BLE001 - a background task must always report an outcome
            log.exception("could not prepare the embedder")
            outcome = FAILED
        with self._lock:
            if embedder is self._embedder:
                self._state = outcome
        done.set()


def _onnx_status(wanted: str) -> dict[str, Any]:
    if wanted == "onnx":
        detail = "The built-in ONNX embedder is selected."
    else:
        detail = "Ollama is not installed; the built-in ONNX embedder is used instead."
    return {
        "backend": "onnx",
        "model": ONNX_MODEL,
        "space": ONNX_SPACE,
        "installed": wanted == "onnx",
        "running": False,
        "model_present": Path.home().joinpath(".cache", *ONNX_CACHE).is_file(),
        "detail": detail,
    }


def _ollama_status(embedder: Any, state: str) -> dict[str, Any]:
    info = embedder.status()
    detail = str(info.get("detail", ""))
    if state == FAILED:
        detail = FAILED_DETAIL
    elif state == PREPARING and "downloading" not in detail:
        detail = PREPARING_DETAIL
    return {
        "backend": "ollama",
        "model": str(info.get("model", "")),
        "space": str(info.get("space", "")),
        "installed": bool(info.get("installed")),
        "running": bool(info.get("running")),
        "model_present": bool(info.get("model_present")),
        "detail": detail,
    }
