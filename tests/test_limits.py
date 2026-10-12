import json

import httpx
import pytest
from fastapi.testclient import TestClient

from pwndoc_ai.limits import (
    DEFAULT_LIMIT,
    UPLOAD_LIMIT,
    ApiHeadersMiddleware,
    BodyLimitMiddleware,
    BodyTooLarge,
    limit_for,
)
from pwndoc_ai.server import create_app

CSRF = {"X-Requested-With": "pwndoc-ai"}


class Recorder:
    """Drives an ASGI app with a scripted request body and records what it sent."""

    def __init__(self, app):
        self.app = app
        self.sent: list[dict] = []

    async def call(self, method, path, chunks, content_length=None):
        headers = [(b"host", b"gw")]
        if content_length is not None:
            headers.append((b"content-length", str(content_length).encode()))
        scope = {"type": "http", "method": method, "path": path, "headers": headers}
        queue = list(chunks)

        async def receive():
            if not queue:
                return {"type": "http.disconnect"}
            body = queue.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(queue)}

        async def send(message):
            self.sent.append(message)

        await self.app(scope, receive, send)
        return self

    @property
    def status(self):
        return next(m["status"] for m in self.sent if m["type"] == "http.response.start")

    @property
    def headers(self):
        start = next(m for m in self.sent if m["type"] == "http.response.start")
        return dict(start["headers"])


def make_inner(consume=True, start_first=False):
    state = {"called": False, "read": 0}

    async def inner(scope, receive, send):
        state["called"] = True
        if start_first:
            await send({"type": "http.response.start", "status": 200, "headers": []})
        while consume:
            message = await receive()
            if message["type"] != "http.request":
                break
            state["read"] += len(message["body"])
            if not message.get("more_body"):
                break
        if not start_first:
            await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return inner, state


def test_limit_selection():
    assert limit_for("POST", "/__ai/api/rag/documents") == UPLOAD_LIMIT
    assert limit_for("PUT", "/__ai/api/settings") == DEFAULT_LIMIT
    assert limit_for("GET", "/__ai/api/rag/documents") == DEFAULT_LIMIT


async def test_declared_content_length_over_limit_is_rejected_before_the_app_runs():
    inner, state = make_inner()
    result = await Recorder(BodyLimitMiddleware(inner)).call(
        "POST", "/__ai/api/jobs", [b"x"], content_length=DEFAULT_LIMIT + 1
    )
    assert result.status == 413 and not state["called"]
    body = json.loads(next(m for m in result.sent if m["type"] == "http.response.body")["body"])
    assert "larger than" in body["detail"]


async def test_chunked_body_is_cut_off_while_streaming():
    inner, state = make_inner()
    chunks = [b"x" * 1024] * (DEFAULT_LIMIT // 1024 + 2)  # no Content-Length declared
    result = await Recorder(BodyLimitMiddleware(inner)).call("POST", "/__ai/api/jobs", chunks)
    assert result.status == 413
    assert state["read"] <= DEFAULT_LIMIT  # the app never saw more than the limit


async def test_body_within_limit_passes_through():
    inner, state = make_inner()
    result = await Recorder(BodyLimitMiddleware(inner)).call(
        "POST", "/__ai/api/jobs", [b"x" * 1000], content_length=1000
    )
    assert result.status == 200 and state["read"] == 1000


async def test_upload_route_gets_the_larger_limit():
    inner, _ = make_inner()
    big = DEFAULT_LIMIT * 5
    ok = await Recorder(BodyLimitMiddleware(inner)).call(
        "POST", "/__ai/api/rag/documents", [b"x"], content_length=big
    )
    assert ok.status == 200
    too_big = await Recorder(BodyLimitMiddleware(inner)).call(
        "POST", "/__ai/api/rag/documents", [b"x"], content_length=UPLOAD_LIMIT + 1
    )
    assert too_big.status == 413


async def test_proxied_paths_are_not_limited():
    inner, state = make_inner()
    result = await Recorder(BodyLimitMiddleware(inner)).call(
        "POST", "/api/audits", [b"x" * 10], content_length=DEFAULT_LIMIT * 10
    )
    assert result.status == 200 and state["called"]


async def test_overflow_after_the_response_started_is_reraised_not_double_sent():
    inner, _ = make_inner(start_first=True)
    chunks = [b"x" * 1024] * (DEFAULT_LIMIT // 1024 + 2)
    with pytest.raises(BodyTooLarge):
        await Recorder(BodyLimitMiddleware(inner)).call("POST", "/__ai/api/jobs", chunks)


async def test_non_http_scopes_pass_through():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    await BodyLimitMiddleware(inner)({"type": "lifespan"}, None, None)
    await ApiHeadersMiddleware(inner)({"type": "websocket", "path": "/__ai/x"}, None, None)
    assert seen == ["lifespan", "websocket"]


async def test_api_headers_added_only_under_ai_prefix_without_duplicates():
    inner, _ = make_inner()
    ai = await Recorder(ApiHeadersMiddleware(inner)).call("GET", "/__ai/api/status", [])
    assert ai.headers[b"x-content-type-options"] == b"nosniff"
    assert ai.headers[b"referrer-policy"] == b"same-origin"
    other = await Recorder(ApiHeadersMiddleware(inner)).call("GET", "/api/audits", [])
    assert b"x-content-type-options" not in other.headers

    async def preset(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"x-content-type-options", b"nosniff")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    again = await Recorder(ApiHeadersMiddleware(preset)).call("GET", "/__ai/static/a", [])
    start = next(m for m in again.sent if m["type"] == "http.response.start")
    assert [n for n, _ in start["headers"]].count(b"x-content-type-options") == 1


# -- through the real app --------------------------------------------------------------------
def upstream(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json={"status": "error", "datas": "Invalid token"})


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, transport=httpx.MockTransport(upstream), env={})
    with TestClient(app) as test_client:
        yield test_client


def test_unauthenticated_oversized_settings_put_is_413_not_parsed(client):
    response = client.put(
        "/__ai/api/settings", headers=CSRF, content=b"{" + b" " * (DEFAULT_LIMIT + 10) + b"}"
    )
    assert response.status_code == 413
    assert response.headers["x-content-type-options"] == "nosniff"


def test_unauthenticated_oversized_upload_is_413_before_auth(client):
    big = b"x" * (UPLOAD_LIMIT + 1)
    response = client.post("/__ai/api/rag/documents", headers=CSRF, files={"file": ("a.txt", big)})
    assert response.status_code == 413


def test_small_unauthenticated_request_still_gets_the_normal_401(client):
    assert client.put("/__ai/api/settings", headers=CSRF, json={}).status_code == 401


def test_hardening_headers_on_api_and_static_but_not_proxied(client):
    api = client.get("/__ai/api/status")
    static = client.get("/__ai/static/inject.js")
    proxied = client.get("/anything")
    for response in (api, static):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "same-origin"
    assert "referrer-policy" not in proxied.headers
