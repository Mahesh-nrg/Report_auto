"""Pure-ASGI guards for the ``/__ai`` surface: request-size limits and response headers.

FastAPI parses request bodies before it resolves dependencies, so without a guard an
unauthenticated caller could make the gateway spool arbitrarily large uploads to disk or memory
before being answered 401. These guards run first and reject oversized bodies early (by
``Content-Length``) or while streaming (chunked bodies).
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
UPLOAD_LIMIT = 21 * 1024 * 1024  # the 20 MB file cap plus multipart framing
API_HEADERS = ((b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin"))


class BodyTooLarge(Exception):  # noqa: N818 - control-flow signal, not an error type
    pass


def limit_for(method: str, path: str) -> int:
    return UPLOAD_LIMIT if method == "POST" and path == UPLOAD_PATH else DEFAULT_LIMIT


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

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(API_PREFIX):
            await self.app(scope, receive, send)
            return
        limit = limit_for(scope["method"], scope["path"])
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

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = {name.lower() for name, _ in message.get("headers", [])}
                extra = [(n, v) for n, v in API_HEADERS if n not in present]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, with_headers)
