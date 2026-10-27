"""Keeps a slow or broken embedder from stalling a report run.

:class:`BoundedEmbed` wraps ``embedder.embed`` with

* a deadline: the call runs in a daemon thread and :class:`EmbedderError` is raised when it does
  not answer in ``timeout`` seconds (the thread ends when the embedder's own request timeout
  does, and a late answer is discarded), and
* a cool-down: after a failure the embedder is not asked again for ``COOLDOWN_SECONDS``, so the
  next findings degrade at once instead of each waiting out the same timeout.

It never creates, starts or stops an embedder: the embedder object serialises its own server.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from typing import Any, Protocol

from .embedder import EmbedderError

EMBED_TIMEOUT = 120.0  # seconds one embed call may take (a cold start of the server included)
COOLDOWN_SECONDS = 60.0


class Embeds(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _now() -> float:
    return time.monotonic()


class BoundedEmbed:
    """Callable ``texts -> vectors`` with a deadline and a failure cool-down."""

    def __init__(
        self, embedder: Embeds, timeout: float = EMBED_TIMEOUT, cooldown: float = COOLDOWN_SECONDS
    ):
        self._embedder = embedder
        self._timeout = timeout
        self._cooldown = cooldown
        self._lock = threading.Lock()
        self._down_until = 0.0

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        with self._lock:
            cooling = _now() < self._down_until
        if cooling:
            raise EmbedderError("the embedder failed recently and is being left alone")
        try:
            return self._run(list(texts))
        except Exception:
            with self._lock:
                self._down_until = _now() + self._cooldown
            raise

    def _run(self, texts: list[str]) -> list[list[float]]:
        outcome: dict[str, Any] = {}

        def work() -> None:
            try:
                outcome["vectors"] = self._embedder.embed(texts)
            except Exception as exc:  # noqa: BLE001 - handed to the caller below
                outcome["error"] = exc

        thread = threading.Thread(target=work, daemon=True, name="research-embed")
        thread.start()
        thread.join(self._timeout)
        if thread.is_alive():
            raise EmbedderError("the embedder did not answer in time")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["vectors"]
