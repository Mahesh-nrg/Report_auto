import asyncio
import threading

import httpx
import pytest
import websockets
from fastapi.testclient import TestClient
from websockets.asyncio.server import serve

from pwndoc_ai.proxy import (
    INJECTION,
    inject_assets,
    outbound_headers,
    rewrite_location,
    rewrite_set_cookie,
)
from pwndoc_ai.server import create_app

UPSTREAM = "https://localhost:8443"
PAGE = b"<html><body><div id=app></div></body></html>"
seen: list[httpx.Request] = []


def upstream(request: httpx.Request) -> httpx.Response:
    seen.append(request)
    path = request.url.path
    if path == "/":
        return httpx.Response(
            200, content=PAGE, headers={"content-type": "text/html; charset=utf-8"}
        )
    if path == "/login-cookie":
        return httpx.Response(
            200,
            json={"ok": True},
            headers=[
                (
                    "set-cookie",
                    "token=JWT%20abc; Path=/; Domain=localhost; HttpOnly; Secure; SameSite=Strict",
                ),
                ("set-cookie", "refreshToken=r1; Path=/api/users/refreshtoken; Secure"),
            ],
        )
    if path == "/redirect":
        return httpx.Response(302, headers={"location": f"{UPSTREAM}/audits"})
    if path == "/echo":
        return httpx.Response(
            200, json={"body": request.content.decode(), "method": request.method}
        )
    if path == "/app.js":
        return httpx.Response(
            200, content=b"console.log(1)", headers={"content-type": "text/javascript"}
        )
    return httpx.Response(404, json={"status": "error"})


@pytest.fixture
def client(tmp_path):
    seen.clear()
    app = create_app(tmp_path, transport=httpx.MockTransport(upstream), env={})
    with TestClient(app, base_url="http://gateway.lan:8080") as test_client:
        yield test_client


def test_html_gets_assets_injected_before_body_end(client):
    response = client.get("/", headers={"accept": "text/html"})
    assert response.status_code == 200
    assert response.content.endswith(INJECTION + b"</body></html>")
    assert int(response.headers["content-length"]) == len(response.content)
    assert seen[-1].headers["accept-encoding"] == "identity"
    assert seen[-1].headers["host"] == "localhost:8443"


def test_inject_is_idempotent_and_handles_missing_body_tag():
    once = inject_assets(b"<html></body>")
    assert inject_assets(once) == once
    assert inject_assets(b"<p>x</p>").endswith(INJECTION)


def test_non_html_passes_through_untouched(client):
    response = client.get("/app.js")
    assert response.content == b"console.log(1)"


def test_secure_dropped_on_http_and_domain_removed(client):
    response = client.get("/login-cookie")
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert "Secure" not in cookies[0] and "Domain" not in cookies[0]
    assert "HttpOnly" in cookies[0] and "SameSite=Strict" in cookies[0]
    assert cookies[0].startswith("token=JWT%20abc")


def test_secure_kept_when_served_over_https(tmp_path):
    app = create_app(tmp_path, transport=httpx.MockTransport(upstream), env={})
    with TestClient(app, base_url="https://gateway.lan") as https_client:
        cookies = https_client.get("/login-cookie").headers.get_list("set-cookie")
    assert "Secure" in cookies[0]
    assert "Domain" not in cookies[0]


def test_location_rewritten_to_public_origin(client):
    response = client.get("/redirect", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "http://gateway.lan:8080/audits"


def test_request_body_origin_and_referer_forwarded(client):
    response = client.post(
        "/echo",
        content=b'{"a":1}',
        headers={"origin": "http://gateway.lan:8080", "referer": "http://gateway.lan:8080/login"},
    )
    assert response.json() == {"body": '{"a":1}', "method": "POST"}
    assert seen[-1].headers["origin"] == UPSTREAM
    assert seen[-1].headers["referer"] == f"{UPSTREAM}/login"


def test_upstream_status_is_preserved(client):
    assert client.get("/nope").status_code == 404


def test_unreachable_upstream_is_502(tmp_path):
    def down(request):
        raise httpx.ConnectError("refused")

    app = create_app(tmp_path, transport=httpx.MockTransport(down), env={})
    with TestClient(app) as c:
        response = c.get("/")
    assert response.status_code == 502
    assert "unreachable" in response.json()["detail"]


def test_unknown_ai_paths_are_not_proxied(client):
    assert client.get("/__ai/whatever").status_code == 404
    assert not seen


def test_pure_helpers():
    assert rewrite_set_cookie("a=1; Secure; Path=/; Domain=x", False) == "a=1; Path=/"
    assert rewrite_set_cookie("a=1; Secure; Path=/", True) == "a=1; Secure; Path=/"
    assert rewrite_location("https://u/x", "https://u", "http://p:1") == "http://p:1/x"
    assert rewrite_location("https://other/x", "https://u", "http://p:1") == "https://other/x"
    assert rewrite_location("https://uu/x", "https://u", "http://p:1") == "https://uu/x"
    out = dict(
        outbound_headers(
            [("Connection", "close"), ("Cookie", "a=b")], "https://u:1", "http://p", False
        )
    )
    assert "Connection" not in out and out["Cookie"] == "a=b" and out["host"] == "u:1"


# -- websocket ---------------------------------------------------------------------------------
@pytest.fixture
def ws_upstream():
    """Echo websocket server on a background loop; yields its port and the received handshake."""
    handshake: dict = {}
    ready = threading.Event()
    state: dict = {}

    async def echo(connection):
        handshake["cookie"] = connection.request.headers.get("Cookie")
        async for message in connection:
            await connection.send(f"echo:{message}" if isinstance(message, str) else message)

    async def main():
        async with serve(echo, "127.0.0.1", 0) as server:
            state["port"] = server.sockets[0].getsockname()[1]
            state["stop"] = asyncio.Event()
            state["loop"] = asyncio.get_running_loop()
            ready.set()
            await state["stop"].wait()

    thread = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
    thread.start()
    assert ready.wait(5)
    yield state["port"], handshake
    state["loop"].call_soon_threadsafe(state["stop"].set)
    thread.join(5)


def test_websocket_is_pumped_both_ways_with_cookie(tmp_path, ws_upstream):
    port, handshake = ws_upstream
    app = create_app(tmp_path, env={})
    app.state.gateway.store.update({"pwndoc_url": f"http://127.0.0.1:{port}"})
    with TestClient(app) as test_client:
        with test_client.websocket_connect(
            "/socket.io/?EIO=4", headers={"cookie": "token=JWT%20x"}
        ) as ws:
            ws.send_text("hi")
            assert ws.receive_text() == "echo:hi"
            ws.send_bytes(b"\x01\x02")
            assert ws.receive_bytes() == b"\x01\x02"
    assert handshake["cookie"] == "token=JWT%20x"


def test_websocket_failure_rejects_handshake_so_polling_can_take_over(tmp_path):
    app = create_app(tmp_path, env={})
    app.state.gateway.store.update({"pwndoc_url": "http://127.0.0.1:1"})  # nothing listening
    with TestClient(app) as test_client:
        with pytest.raises(Exception):  # noqa: B017 - starlette raises WebSocketDisconnect
            with test_client.websocket_connect("/socket.io/?EIO=4"):
                pass


_ = websockets  # imported to fail early if the dependency is missing


# -- security regressions ------------------------------------------------------------------------
def _cookie_spy_app(tmp_path):
    """Upstream that hands out a session cookie on /login-cookie and records Cookie headers."""
    cookies_seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cookies_seen.append(request.headers.get("cookie"))
        if request.url.path == "/login-cookie":
            return httpx.Response(
                200,
                json={"ok": True},
                headers=[("set-cookie", "token=JWT%20victim; Path=/; HttpOnly; Secure")],
            )
        return httpx.Response(200, json={"ok": True})

    app = create_app(tmp_path, transport=httpx.MockTransport(handler), env={})
    return app, cookies_seen


def test_one_users_login_cookie_is_never_replayed_to_a_cookieless_caller(tmp_path):
    """Regression: the pooled upstream client must not keep a cookie jar shared by all users."""
    app, cookies_seen = _cookie_spy_app(tmp_path)
    # One app context for the whole test: leaving it would close (and so reset) the shared pool.
    with TestClient(app, base_url="http://gateway.lan:8080") as browser:
        assert browser.get("/login-cookie").status_code == 200  # victim signs in
        browser.cookies.clear()  # the next caller is a different, cookieless browser
        browser.get("/api/users/me")
    assert cookies_seen[-1] is None
    assert not any(c and "victim" in c for c in cookies_seen[1:])


def test_each_users_own_cookie_is_still_forwarded_unmangled(tmp_path):
    app, cookies_seen = _cookie_spy_app(tmp_path)
    with TestClient(app, base_url="http://gateway.lan:8080") as c:
        c.get("/api/users/me", headers={"cookie": "token=JWT%20mine"})
    assert cookies_seen[-1] == "token=JWT%20mine"


CSP = "base-uri 'self'; upgrade-insecure-requests; block-all-mixed-content; frame-ancestors 'none'"


def csp_app(tmp_path, value=CSP):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=PAGE,
            headers={"content-type": "text/html", "content-security-policy": value},
        )

    return create_app(tmp_path, transport=httpx.MockTransport(handler), env={})


def test_csp_https_only_directives_dropped_for_plain_http_browsers(tmp_path):
    with TestClient(csp_app(tmp_path), base_url="http://gateway.lan:8080") as c:
        csp = c.get("/", headers={"accept": "text/html"}).headers["content-security-policy"]
    assert csp == "base-uri 'self'; frame-ancestors 'none'"


def test_csp_kept_intact_when_the_browser_is_on_https(tmp_path):
    with TestClient(csp_app(tmp_path), base_url="https://gateway.lan:8443") as c:
        csp = c.get("/", headers={"accept": "text/html"}).headers["content-security-policy"]
    assert csp == CSP


def test_csp_header_removed_when_nothing_is_left(tmp_path):
    app = csp_app(tmp_path, "upgrade-insecure-requests; block-all-mixed-content")
    with TestClient(app, base_url="http://gateway.lan:8080") as c:
        assert "content-security-policy" not in c.get("/", headers={"accept": "text/html"}).headers


def test_relax_csp_unit():
    from pwndoc_ai.proxy import relax_csp

    assert relax_csp("Upgrade-Insecure-Requests") == ""
    assert relax_csp("default-src 'self'; upgrade-insecure-requests") == "default-src 'self'"
    assert relax_csp("img-src 'self' data:") == "img-src 'self' data:"


async def test_pump_cancels_both_directions_and_closes_when_the_handler_is_cancelled():
    from pwndoc_ai.proxy import _pump

    never = asyncio.Event()

    class FakeSocket:
        closed = False

        async def receive(self):
            await never.wait()

        async def send_text(self, text):  # pragma: no cover - nothing is ever delivered
            pass

        async def close(self):
            self.closed = True

    class FakeUpstream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await never.wait()

        async def send(self, payload):  # pragma: no cover
            pass

    socket = FakeSocket()
    handler = asyncio.create_task(_pump(socket, FakeUpstream()))
    await asyncio.sleep(0.05)
    assert len(asyncio.all_tasks()) == 4  # test + handler + the two pumps
    handler.cancel()
    await asyncio.gather(handler, return_exceptions=True)
    await asyncio.sleep(0)
    assert len(asyncio.all_tasks()) == 1  # no orphaned pump tasks left holding sockets
    assert socket.closed
