"""Routes of the local document library (RAG): upload, list, delete, index, embedder.

Administrators only, and every method (GET included) needs the ``X-Requested-With`` header, so a
page on another site cannot drive them with the user's cookie. Uploading only stores a file; an
explicit, incremental indexing run (``POST /rag/index``) is what reads and embeds documents.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .api_deps import AdminDep, GatewayDep, limited, require_csrf_header
from .gateway import Gateway
from .indexer import IndexerBusy
from .rag import RagUnavailable, safe_filename
from .upload_stream import receive_upload
from .uploads import StoredFile, UploadError

log = logging.getLogger(__name__)

MAX_INDEX_NAMES = 5000
PREPARE_WAIT_SECONDS = 100.0  # the call returns after this long even if the download goes on
UNAVAILABLE = "local RAG is unavailable: pip install 'pwndoc-ai[rag]'"
INDEX_BUSY = "indexing is already running"
INDEX_UNREADABLE = "could not read the document index; see the gateway log"

router = APIRouter(prefix="/__ai/api/rag", dependencies=[Depends(require_csrf_header)])


class IndexRequest(BaseModel):
    """Optional body of POST /rag/index: only these stored files (default: every stored file)."""

    names: list[str] | None = Field(default=None, min_length=1, max_length=MAX_INDEX_NAMES)


def merge_documents(
    stored: Sequence[StoredFile],
    indexed: Sequence[dict[str, Any]],
    outcomes: Mapping[str, Mapping[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Stored files joined with what the index knows (unindexed files show ``indexed: false``).

    ``state`` is ``indexed``, ``pending`` (waiting for an indexing run), or, for a stored file an
    indexing run could not use, ``duplicate`` (its content is indexed under another name) or
    ``failed`` (with an ``error``). An indexed document whose file is gone is still listed
    (size 0), so it can be deleted.
    """
    known = {str(doc["name"]): doc for doc in indexed}
    notes = outcomes or {}
    rows = []
    for entry in stored:
        rows.append(
            _row(entry.name, entry.size, known.pop(entry.name, None), notes.get(entry.name))
        )
    rows.extend(_row(name, 0, doc, None) for name, doc in known.items())
    return sorted(rows, key=lambda row: row["name"])


def _row(
    name: str, size: int, doc: dict[str, Any] | None, outcome: Mapping[str, str] | None
) -> dict[str, Any]:
    chunks = int(doc.get("chunks", 0) or 0) if doc else 0
    row = {
        "name": name,
        "size": size,
        "chunks": chunks,
        "low_text": bool(doc and doc.get("low_text")),
        "indexed": chunks > 0,
        "state": "indexed" if chunks > 0 else "pending",
    }
    if chunks == 0 and outcome:
        row["state"] = outcome.get("status", "pending")
        row["error"] = outcome.get("error", "")
    return row


def _outcomes(index: Any) -> dict[str, dict[str, str]]:
    """Why stored files are not indexed (an index without that memory knows nothing of it)."""
    outcomes = getattr(index, "outcomes", None)
    if not callable(outcomes):
        return {}
    try:
        return dict(outcomes())
    except Exception:  # noqa: BLE001 - a note that cannot be read only costs the file its reason
        log.exception("could not read the document outcomes")
        return {}


def _need_index(gw: Gateway) -> Any:
    index = gw.rag_index()
    if index is None:
        raise HTTPException(400, UNAVAILABLE)
    return index


@router.get("/documents")
def list_documents(identity: AdminDep, gw: GatewayDep) -> dict[str, Any]:
    """Stored documents with their index state: uploaded-but-not-indexed files are listed too."""
    with limited(gw, "rag_list", identity.username):
        index = gw.rag_index()
        store = gw.documents()
        if index is None or store is None:
            return {"available": False, "documents": []}
        try:
            indexed = index.list_documents()
        except Exception as exc:  # noqa: BLE001 - a broken vector store: generic answer, full log
            log.exception("could not list the indexed documents")
            raise HTTPException(500, INDEX_UNREADABLE) from exc
        answer: dict[str, Any] = {
            "available": True,
            "documents": merge_documents(store.list_files(), indexed, _outcomes(index)),
            "max_upload_bytes": store.max_bytes,
        }
        if hasattr(index, "embedder_name"):
            answer["embedder"] = index.embedder_name
        return answer


@router.post("/documents")
async def upload_document(request: Request, identity: AdminDep, gw: GatewayDep) -> dict[str, Any]:
    """Store ONE uploaded file (multipart field ``file``); it is not indexed here."""
    store = await run_in_threadpool(gw.documents)  # may open the index the first time
    if store is None:
        raise HTTPException(400, UNAVAILABLE)
    with limited(gw, "rag_upload", identity.username):
        try:
            stored = await receive_upload(request, store)
        except UploadError as exc:
            raise HTTPException(exc.status, exc.message) from exc
    return stored.as_dict()


@router.delete("/documents/{name}")
def delete_document(name: str, identity: AdminDep, gw: GatewayDep) -> dict[str, Any]:
    with limited(gw, "rag_admin", identity.username):
        index = _need_index(gw)
        if gw.indexing_now():  # chunks would come back when the running file finishes
            raise HTTPException(409, INDEX_BUSY)
        safe = safe_filename(name)
        index.remove(safe)
    return {"deleted": safe}


def _selected_names(body: IndexRequest | None, stored: set[str]) -> tuple[str, ...] | None:
    if body is None or body.names is None:
        return None
    names = tuple(dict.fromkeys(body.names))
    if any(safe_filename(name) != name for name in names):
        raise HTTPException(400, "invalid document name")
    if not set(names) <= stored:
        raise HTTPException(400, "unknown document name")
    return names


@router.post("/index")
def start_indexing(
    identity: AdminDep, gw: GatewayDep, body: Annotated[IndexRequest | None, Body()] = None
) -> dict[str, Any]:
    """Start the one-time, incremental indexing run in the background (409 while one runs)."""
    with limited(gw, "rag_admin", identity.username):
        store = gw.documents()
        if store is None:
            raise HTTPException(400, UNAVAILABLE)
        stored = {entry.name for entry in store.list_files()}
        names = _selected_names(body, stored)
        try:
            gw.indexer().start(names)
        except IndexerBusy as exc:
            raise HTTPException(409, INDEX_BUSY) from exc
        except RagUnavailable as exc:
            raise HTTPException(400, UNAVAILABLE) from exc
        except ValueError as exc:  # a document vanished between listing and starting
            raise HTTPException(400, "unknown document name") from exc
    return {"started": True, "total": len(stored) if names is None else len(names)}


@router.get("/index/status")
def indexing_status(identity: AdminDep, gw: GatewayDep) -> dict[str, Any]:
    with limited(gw, "rag_status", identity.username):
        return gw.indexer().status()


@router.post("/index/cancel")
def cancel_indexing(identity: AdminDep, gw: GatewayDep) -> dict[str, Any]:
    with limited(gw, "rag_admin", identity.username):
        indexer = gw.indexer()
        indexer.cancel()
        return indexer.status()


@router.get("/embedder")
def embedder_status(identity: AdminDep, gw: GatewayDep) -> dict[str, Any]:
    with limited(gw, "rag_status", identity.username):
        return gw.embedders.status()


@router.post("/embedder/prepare")
def prepare_embedder(identity: AdminDep, gw: GatewayDep) -> dict[str, Any]:
    """Start the embedder and download its model when missing.

    The work runs in the background; the call waits a bounded time for it, so a model that is
    already on disk answers ``ready`` and a long download answers ``preparing`` (poll the status).
    """
    with limited(gw, "rag_prepare", identity.username):
        return gw.embedders.prepare(wait=PREPARE_WAIT_SECONDS)
