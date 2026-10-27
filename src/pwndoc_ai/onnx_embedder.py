"""ChromaDB's built-in ONNX embedder as an embedder object (vector space ``minilm-l6-v2``).

Used to rank internet search results when no Ollama is there: the local index already uses this
model (ChromaDB embeds with it by default), and Ollama's ``all-minilm`` gives the same vectors, so
one ranking pipeline serves both. The model is loaded on first use (ChromaDB downloads it once if
it is missing), never at construction, and a failure is an :class:`EmbedderError` like any other
embedder's, which the research pipeline already turns into a degraded (truncated) result.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from typing import Any

from .embedder import MINILM_SPACE, EmbedderError

log = logging.getLogger(__name__)

ONNX_NAME = "onnx:all-MiniLM-L6-v2"


def _chroma_function() -> Any:
    try:
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
    except ImportError as exc:
        raise EmbedderError("chromadb is not installed (pip install 'pwndoc-ai[rag]')") from exc
    return DefaultEmbeddingFunction()


class OnnxEmbedder:
    """``embed(texts)`` with the built-in model; the same surface as the Ollama embedder."""

    name = ONNX_NAME
    space = MINILM_SPACE

    def __init__(self, factory: Callable[[], Any] = _chroma_function):
        self._factory = factory
        self._function: Any | None = None
        self._lock = threading.Lock()

    def _loaded(self) -> Any:
        with self._lock:
            if self._function is None:
                self._function = self._factory()
            return self._function

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        try:
            vectors = self._loaded()(list(texts))
            return [[float(x) for x in vector] for vector in vectors]
        except EmbedderError:
            raise
        except Exception as exc:  # noqa: BLE001 - the model, the download or onnxruntime failed
            log.warning("the built-in embedder failed (%s)", type(exc).__name__)
            raise EmbedderError(f"the built-in embedder failed ({type(exc).__name__})") from None

    def close(self) -> None:
        """Nothing to stop: the model lives in this process."""
