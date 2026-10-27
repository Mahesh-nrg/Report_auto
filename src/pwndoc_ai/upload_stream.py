"""Receive ONE multipart file straight into the documents folder, without buffering it.

FastAPI's own form parsing spools every upload to a temporary file before the route (and its
authentication) runs. For books of hundreds of megabytes that is both slow and an easy way for an
anonymous caller to fill the disk, so the upload route declares no body at all, lets the
authentication dependencies run first, and reads the request stream here. The multipart framing
is parsed incrementally; the file part goes into an :class:`pwndoc_ai.uploads.UploadSink`.

Exactly one part is accepted, it must be the field ``file`` with a file name, and the file is only
moved into place after the whole body was read, so a second part (or a broken body) stores nothing.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import anyio
from fastapi import Request
from starlette.concurrency import run_in_threadpool

from .limits import UPLOAD_PATH, BodyLimitMiddleware, Scope
from .uploads import (
    HTTP_BAD_REQUEST,
    HTTP_TIMEOUT,
    HTTP_UNSUPPORTED,
    DocumentStore,
    StoredDocument,
    UploadError,
    UploadSink,
    upload_body_limit,
)

try:  # python-multipart >= 0.0.13 installs ``python_multipart``; older ones ``multipart``
    from python_multipart.multipart import (
        MultipartParseError,
        MultipartParser,
        parse_options_header,
    )
except ImportError:  # pragma: no cover - depends on the installed python-multipart
    from multipart.multipart import MultipartParseError, MultipartParser, parse_options_header

FIELD_NAME = b"file"
FEED_BYTES = 1024 * 1024  # bytes gathered before one hop to a worker thread
IDLE_SECONDS = 60.0  # a client that sends nothing for this long loses its upload slot
MAX_BOUNDARY = 200
MAX_HEADER_BYTES = 4224
MAX_HEADERS = 8
NOT_MULTIPART = "send the document as multipart/form-data in the field 'file'"
TOO_LONG = "the upload took too long and was cancelled"
ONE_FILE = "send exactly one file per request in the field 'file'"


def _boundary(content_type: str) -> bytes:
    kind, params = parse_options_header(content_type)
    boundary = params.get(b"boundary", b"")
    if kind.lower() != b"multipart/form-data" or not 0 < len(boundary) <= MAX_BOUNDARY:
        raise UploadError(HTTP_UNSUPPORTED, NOT_MULTIPART)
    return boundary


class _SingleFileReader:
    """Feeds the request body to the multipart parser and the file part to an upload sink."""

    def __init__(self, boundary: bytes, store: DocumentStore):
        self._store = store
        self._sink: UploadSink | None = None
        self._parts = 0
        self._ended = False  # the closing boundary was seen: the body is complete
        self._field = b""
        self._value = b""
        self._headers: dict[bytes, bytes] = {}
        self._parser = MultipartParser(
            boundary,
            {
                "on_part_begin": self._part_begin,
                "on_header_field": self._header_field,
                "on_header_value": self._header_value,
                "on_header_end": self._header_end,
                "on_headers_finished": self._headers_finished,
                "on_part_data": self._part_data,
                "on_end": self._end,
            },
            max_header_count=MAX_HEADERS,
            max_header_size=MAX_HEADER_BYTES,
        )

    def feed(self, data: bytes) -> None:
        try:
            self._parser.write(data)
        except MultipartParseError as exc:
            raise UploadError(HTTP_BAD_REQUEST, "the upload is not valid multipart data") from exc

    def finish(self) -> StoredDocument:
        """The body is complete: move the file into place (nothing was stored before this)."""
        try:
            self._parser.finalize()
        except MultipartParseError as exc:
            raise UploadError(HTTP_BAD_REQUEST, "the upload is incomplete") from exc
        sink = self._sink
        if sink is not None and not self._ended:
            raise UploadError(HTTP_BAD_REQUEST, "the upload is incomplete")
        if sink is None:
            raise UploadError(HTTP_BAD_REQUEST, "no file was sent in the field 'file'")
        return sink.finish()

    def abort(self) -> None:
        if self._sink is not None:
            self._sink.abort()

    # -- parser callbacks -----------------------------------------------------------------
    def _part_begin(self) -> None:
        self._headers = {}
        self._field = self._value = b""

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._field += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def _header_end(self) -> None:
        self._headers[self._field.lower()] = self._value
        self._field = self._value = b""

    def _headers_finished(self) -> None:
        self._parts += 1
        if self._parts > 1:
            raise UploadError(HTTP_BAD_REQUEST, ONE_FILE)
        kind, params = parse_options_header(self._headers.get(b"content-disposition", b""))
        filename = params.get(b"filename", b"")
        if kind.lower() != b"form-data" or params.get(b"name") != FIELD_NAME or not filename:
            raise UploadError(HTTP_BAD_REQUEST, NOT_MULTIPART)
        self._sink = self._store.begin(filename.decode("utf-8", errors="replace"))

    def _end(self) -> None:
        self._ended = True

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        if self._sink is not None:
            self._sink.write(data[start:end])


async def _body(request: Request, deadline: float | None = None) -> AsyncIterator[bytes]:
    """The request body, each piece within ``IDLE_SECONDS`` of the one before and, when a
    ``deadline`` is given, all of it within that many seconds (a byte now and then is no upload)."""
    pieces = request.stream().__aiter__()
    end = None if deadline is None else time.monotonic() + deadline
    while True:
        wait = IDLE_SECONDS
        if end is not None:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise UploadError(HTTP_TIMEOUT, TOO_LONG)
            wait = min(wait, remaining)
        try:
            with anyio.fail_after(wait):
                piece = await pieces.__anext__()
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            late = end is not None and time.monotonic() >= end
            message = TOO_LONG if late else "the upload stalled and was cancelled"
            raise UploadError(HTTP_TIMEOUT, message) from exc
        yield piece


async def receive_upload(
    request: Request, store: DocumentStore, *, deadline: float | None = None
) -> StoredDocument:
    """Stream the request body into ``store``; every error path removes the temporary file.
    ``deadline`` (seconds) bounds the whole transfer, not only the pauses between pieces."""
    reader = _SingleFileReader(_boundary(request.headers.get("content-type", "")), store)
    try:
        buffered = bytearray()
        async for chunk in _body(request, deadline):
            buffered += chunk
            if len(buffered) >= FEED_BYTES:
                await run_in_threadpool(reader.feed, bytes(buffered))
                buffered.clear()
        if buffered:
            await run_in_threadpool(reader.feed, bytes(buffered))
        return await run_in_threadpool(reader.finish)
    except BaseException:  # includes a client that went away and task cancellation
        reader.abort()
        raise


class UploadBodyLimit(BodyLimitMiddleware):
    """:class:`BodyLimitMiddleware` that allows the upload route a whole book.

    The limit is the file cap plus multipart framing (:func:`upload_body_limit`); every other
    ``/__ai`` request keeps the small default limit.
    """

    def limit_for(self, scope: Scope) -> int:
        if scope["method"] == "POST" and scope["path"] == UPLOAD_PATH:
            return upload_body_limit()
        return super().limit_for(scope)
