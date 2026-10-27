"""Plain types of the document index: its errors and the results it reports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


class RagUnavailable(RuntimeError):
    pass


class IndexBusy(RuntimeError):
    """A bulk index run is already in progress."""


class IndexCancelled(RuntimeError):
    """Indexing of a file was stopped between two batches; its finished chunks are kept."""


class DocumentError(ValueError):
    """A document that cannot be indexed; the text is ours, safe to show to an admin."""


FileStatus = Literal["indexed", "skipped", "duplicate", "failed"]


@dataclass(frozen=True)
class FileResult:
    """What indexing one file did."""

    name: str
    status: FileStatus
    chunks_new: int = 0  # chunks embedded and stored by this run
    chunks_reused: int = 0  # chunks that were already stored and are still current
    error: str = ""  # short and generic: no path, no URL


@dataclass(frozen=True)
class SyncResult:
    """Outcome of indexing everything in the documents folder."""

    indexed: tuple[tuple[str, int], ...]  # (file, chunks) of files that were new or changed
    unchanged: tuple[str, ...]  # already indexed, or with no text: nothing was embedded
    failed: tuple[tuple[str, str], ...]  # (file, short reason)


@dataclass(frozen=True)
class Chunk:
    """One retrieved passage and the document it came from."""

    source: str
    text: str


def format_chunks(chunks: list[Chunk]) -> str:
    return "\n\n".join(f"[{chunk.source}]\n{chunk.text}" for chunk in chunks)
