"""Pure-ASGI guards for the ``/__ai`` surface: request-size limits and response headers.

FastAPI parses request bodies before it resolves dependencies, so without a guard an
unauthenticated caller could make the gateway spool arbitrarily large uploads to disk or memory
before being answered 401. These guards run first and reject oversized bodies early (by
``Content-Length``) or while streaming (chunked bodies). The default limit is small; the upload
route's much larger one belongs to :class:`pwndoc_ai.upload_stream.UploadBodyLimit`, which
derives it from the upload file cap (there is no second number to keep in step).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]

API_PREFIX = "/__ai/"
DEFAULT_LIMIT = 1024 * 1024  # 1 MiB: settings, job requests
UPLOAD_PATH = "/__ai/api/rag/documents"
API_HEADERS = ((b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin"))
NO_STORE_PREFIX = "/__ai/api/docx"  # client reports and their text: never kept by a cache
NO_STORE_HEADERS = ((b"cache-control", b"no-store"), (b"pragma", b"no-cache"))


class BodyTooLarge(Exception):  # noqa: N818 - control-flow signal, not an error type
    pass


async def _reject(send: Send, limit: int) -> None:
    body = json.dumps({"detail": f"request body is larger than {limit // 1024} KiB"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), *API_HEADERS],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BodyLimitMiddleware:
    """Reject ``/__ai/*`` request bodies over the limit with 413, before any parsing."""

    def __init__(self, app: App):
        self.app = app

    def limit_for(self, scope: Scope) -> int:
        """The largest body of this request; subclasses may allow some routes more."""
        return DEFAULT_LIMIT

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(API_PREFIX):
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope)
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await _reject(send, limit)
            return
        await self._guarded(scope, receive, send, limit)

    async def _guarded(self, scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        seen = 0
        started = False

        async def counting_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except BodyTooLarge:
            if started:
                raise
            await _reject(send, limit)


class ApiHeadersMiddleware:
    """Add hardening headers to the gateway's own ``/__ai/*`` responses (not to proxied ones)."""

    def __init__(self, app: App):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(API_PREFIX):
            await self.app(scope, receive, send)
            return

        no_store = scope["path"] == NO_STORE_PREFIX or scope["path"].startswith(
            NO_STORE_PREFIX + "/"
        )

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = {name.lower() for name, _ in message.get("headers", [])}
                wanted = (*API_HEADERS, *NO_STORE_HEADERS) if no_store else API_HEADERS
                extra = [(n, v) for n, v in wanted if n not in present]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, with_headers)
