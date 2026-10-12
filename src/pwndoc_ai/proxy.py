"""Reverse proxy to a stock PwnDoc instance that injects the AI assets into HTML pages."""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import AsyncIterator
from http.cookiejar import CookieJar
from urllib.parse import quote, urlparse

import httpx
from fastapi import WebSocket
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from .config import Settings, effective_verify_tls

log = logging.getLogger(__name__)

INJECTION = (
    b'<link rel="stylesheet" href="/__ai/static/inject.css">'
    b'<script src="/__ai/static/inject.js" defer></script>'
)
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)
UPSTREAM_TIMEOUT = 60.0
# Over plain HTTP these CSP directives would make the browser rewrite the gateway's own
# subresource URLs to https:// (or block them), which a plain-HTTP gateway cannot answer.
HTTPS_ONLY_CSP = frozenset({"upgrade-insecure-requests", "block-all-mixed-content"})
WS_OPEN_TIMEOUT = 10.0


# -- pure helpers ----------------------------------------------------------------------------
def inject_assets(body: bytes) -> bytes:
    """Insert the AI stylesheet/script before ``</body>`` (idempotent)."""
    if INJECTION in body:
        return body
    index = body.lower().rfind(b"</body>")
    if index < 0:
        return body + INJECTION
    return body[:index] + INJECTION + body[index:]


def rewrite_set_cookie(value: str, secure_request: bool) -> str:
    """Drop ``Domain`` always and ``Secure`` when the browser reaches us over plain HTTP."""
    first, *attrs = [part.strip() for part in value.split(";")]
    kept = [first]
    for attr in attrs:
        name = attr.split("=", 1)[0].strip().lower()
        if name == "domain" or (name == "secure" and not secure_request):
            continue
        kept.append(attr)
    return "; ".join(kept)


def relax_csp(value: str) -> str:
    """Remove the https-forcing directives from a Content-Security-Policy header value."""
    kept = [
        part.strip()
        for part in value.split(";")
        if part.strip() and part.split()[0].lower() not in HTTPS_ONLY_CSP
    ]
    return "; ".join(kept)


def rewrite_location(value: str, upstream_base: str, public_origin: str) -> str:
    if value == upstream_base or value.startswith(upstream_base + "/"):
        return public_origin + value[len(upstream_base) :]
    return value


def outbound_headers(
    headers: list[tuple[str, str]], upstream_base: str, public_origin: str, wants_html: bool
) -> list[tuple[str, str]]:
    parsed = urlparse(upstream_base)
    upstream_origin = f"{parsed.scheme}://{parsed.netloc}"
    out: list[tuple[str, str]] = []
    for name, value in headers:
        lower = name.lower()
        if lower in HOP_BY_HOP or lower in ("host", "accept-encoding"):
            continue
        if lower == "origin":
            value = upstream_origin
        elif lower == "referer" and value.startswith(public_origin):
            value = upstream_base + value[len(public_origin) :]
        out.append((name, value))
    out.append(("host", parsed.netloc))
    accept = next((v for n, v in headers if n.lower() == "accept-encoding"), None)
    if wants_html:
        out.append(("accept-encoding", "identity"))
    elif accept:
        out.append(("accept-encoding", accept))
    return out


def inbound_headers(
    upstream: httpx.Response,
    upstream_base: str,
    public_origin: str,
    secure_request: bool,
    drop: frozenset[str] = frozenset(),
) -> tuple[dict[str, str], list[str]]:
    """Return (plain headers, set-cookie values) for the browser-facing response."""
    plain: dict[str, str] = {}
    for name, value in upstream.headers.multi_items():
        lower = name.lower()
        if lower in HOP_BY_HOP or lower == "set-cookie" or lower in drop:
            continue
        if lower == "location":
            value = rewrite_location(value, upstream_base, public_origin)
        elif lower == "content-security-policy" and not secure_request:
            value = relax_csp(value)
            if not value:
                continue
        plain[name] = value
    cookies = [
        rewrite_set_cookie(value, secure_request)
        for value in upstream.headers.get_list("set-cookie")
    ]
    return plain, cookies


def public_origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"


def _upstream_url(request: Request, base: str) -> str:
    raw = request.scope.get("raw_path")
    path = raw.decode("latin-1") if raw else quote(request.url.path)
    query = request.url.query
    return f"{base}{path}" + (f"?{query}" if query else "")


# -- HTTP ------------------------------------------------------------------------------------
class _InertJar(CookieJar):
    """A cookie jar that never stores or sends cookies.

    The pooled client is shared by every gateway user. httpx's default jar would remember the
    last ``Set-Cookie`` from PwnDoc and replay it on any later cookieless request, handing one
    user's session to the next caller. Cookies are forwarded explicitly per request instead.
    """

    def extract_cookies(self, response: object, request: object) -> None:  # noqa: D401
        return None

    def add_cookie_header(self, request: object) -> None:
        return None


class UpstreamPool:
    """Lazily created AsyncClients, one per TLS-verification mode."""

    def __init__(self, transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None):
        self._transport = transport
        self._clients: dict[bool, httpx.AsyncClient] = {}

    def get(self, verify: bool) -> httpx.AsyncClient:
        client = self._clients.get(verify)
        if client is None:
            client = httpx.AsyncClient(
                transport=self._transport,
                verify=verify,
                follow_redirects=False,
                timeout=UPSTREAM_TIMEOUT,
                # Pass the jar itself: httpx.Cookies(...) would be copied into a plain CookieJar.
                cookies=_InertJar(),
            )
            self._clients[verify] = client
        return client

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()


async def proxy_http(request: Request, pool: UpstreamPool, settings: Settings) -> Response:
    base = settings.pwndoc_url
    origin = public_origin(request)
    wants_html = "text/html" in request.headers.get("accept", "")
    client = pool.get(effective_verify_tls(settings))
    body = None if request.method in ("GET", "HEAD") else request.stream()
    outgoing = client.build_request(
        request.method,
        _upstream_url(request, base),
        headers=outbound_headers(list(request.headers.items()), base, origin, wants_html),
        content=body,
    )
    try:
        upstream = await client.send(outgoing, stream=True)
    except httpx.HTTPError as exc:
        log.warning("upstream request to %s failed: %s", base, exc)
        return JSONResponse({"detail": "PwnDoc is unreachable from the gateway"}, status_code=502)
    secure = request.url.scheme == "https"
    if "text/html" in upstream.headers.get("content-type", "") and request.method != "HEAD":
        return await _html_response(upstream, base, origin, secure)
    headers, cookies = inbound_headers(upstream, base, origin, secure)
    response = StreamingResponse(
        _raw_chunks(upstream),
        status_code=upstream.status_code,
        headers=headers,
        background=BackgroundTask(upstream.aclose),
    )
    _append_cookies(response, cookies)
    return response


async def _raw_chunks(upstream: httpx.Response) -> AsyncIterator[bytes]:
    """Relay the body untouched (still compressed); tolerate bodies a transport pre-read."""
    if upstream.is_stream_consumed:
        yield upstream.content
        return
    async for chunk in upstream.aiter_raw():
        yield chunk


async def _html_response(
    upstream: httpx.Response, base: str, origin: str, secure: bool
) -> Response:
    try:
        raw = await upstream.aread()
    finally:
        await upstream.aclose()
    headers, cookies = inbound_headers(
        upstream, base, origin, secure, drop=frozenset({"content-length", "content-encoding"})
    )
    content = raw if upstream.status_code in (204, 304) else inject_assets(raw)
    response = Response(content, status_code=upstream.status_code, headers=headers)
    _append_cookies(response, cookies)
    return response


def _append_cookies(response: Response, cookies: list[str]) -> None:
    for value in cookies:
        response.headers.append("set-cookie", value)


# -- WebSocket (socket.io) -------------------------------------------------------------------
def _ssl_context(verify: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # noqa: S502 - stock PwnDoc is self-signed; opt-in
    return context


async def proxy_websocket(websocket: WebSocket, settings: Settings) -> None:
    base = urlparse(settings.pwndoc_url)
    secure = base.scheme == "https"
    query = websocket.url.query
    target = f"{'wss' if secure else 'ws'}://{base.netloc}{websocket.url.path}" + (
        f"?{query}" if query else ""
    )
    cookie = websocket.headers.get("cookie")
    try:
        async with connect(
            target,
            additional_headers={"Cookie": cookie} if cookie else None,
            ssl=_ssl_context(effective_verify_tls(settings)) if secure else None,
            origin=f"{base.scheme}://{base.netloc}",
            subprotocols=websocket.scope.get("subprotocols") or None,
            open_timeout=WS_OPEN_TIMEOUT,
            max_size=None,
        ) as upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)
            await _pump(websocket, upstream)
    except (OSError, TimeoutError, WebSocketException) as exc:
        # Before accept() this rejects the handshake, so socket.io falls back to polling.
        log.info("websocket proxy for %s failed: %s", websocket.url.path, exc)
        await _safe_close(websocket)


async def _pump(websocket: WebSocket, upstream: ClientConnection) -> None:
    async def to_upstream() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            payload = message.get("text")
            if payload is None:
                payload = message.get("bytes")
            if payload is not None:
                await upstream.send(payload)

    async def to_client() -> None:
        async for message in upstream:
            if isinstance(message, str):
                await websocket.send_text(message)
            else:
                await websocket.send_bytes(message)

    tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(to_client())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            error = None if task.cancelled() else task.exception()
            if error and not isinstance(error, ConnectionClosed):
                log.debug("websocket pump ended: %s", error)
    finally:
        # Also runs when this handler itself is cancelled (shutdown, client gone): never leave
        # the two pumps running against open sockets.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _safe_close(websocket)


async def _safe_close(websocket: WebSocket) -> None:
    try:
        await websocket.close()
    except RuntimeError:
        pass  # already closed


__all__ = [
    "INJECTION",
    "UpstreamPool",
    "inject_assets",
    "inbound_headers",
    "outbound_headers",
    "proxy_http",
    "proxy_websocket",
    "relax_csp",
    "rewrite_location",
    "rewrite_set_cookie",
]
