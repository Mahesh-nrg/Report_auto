"""End-to-end test: real gateway + fake HTTPS PwnDoc + fake LLM + real Chromium.

Run:  pytest tests/e2e -q      or      python tests/e2e/test_gateway_e2e.py
Skips cleanly when Playwright or a Chromium binary (CHROMIUM env, default /usr/bin/chromium) is
missing. No Docker, no internet: the web-search feature is disabled in the seeded config.
"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
expect, sync_playwright = sync_api.expect, sync_api.sync_playwright

CHROMIUM = os.environ.get("CHROMIUM", "/usr/bin/chromium")
if not Path(CHROMIUM).exists():
    pytest.skip(f"Chromium not found at {CHROMIUM}", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

AUDIT = "a" * 24
FINDING = "b" * 24
IMG = '<img src="0123456789abcdef01234567" alt="1.burp.png">'
SECRET_URL = "https://secret.internal.example/admin"
USERS = {  # username -> (password, role, token cookie value as PwnDoc sends it)
    "admin": ("admin-pw", "admin", "JWT%20admin-jwt"),
    "pentester": ("user-pw", "user", "JWT%20user-jwt"),
}
BY_TOKEN = {tok: (name, role) for name, (_, role, tok) in USERS.items()}
BINARY_ASSET = bytes(range(256)) * 64


def me_payload(username: str, role: str) -> dict:
    """What GET /api/users/me returns. Current PwnDoc sends ``roles`` (a list); releases before
    1.x sent ``role`` (a string). The admin uses the new shape and the plain user the old one,
    so a browser session exercises both."""
    if role == "admin":
        return {"username": username, "roles": ["user", "admin"]}
    return {"username": username, "role": role}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Server:
    """A background ThreadingHTTPServer that records every request it receives."""

    def __init__(self, handler: type[BaseHTTPRequestHandler], tls: tuple[str, str] | None = None):
        self.requests: list[dict] = []
        handler.owner = self  # type: ignore[attr-defined]
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*tls)
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_port
        self.scheme = "https" if tls else "http"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"{self.scheme}://127.0.0.1:{self.port}"

    def calls(self, method: str, prefix: str) -> list[dict]:
        return [r for r in self.requests if r["method"] == method and r["path"].startswith(prefix)]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class Base(BaseHTTPRequestHandler):
    owner: Server

    def log_message(self, *args: object) -> None:  # silence
        pass

    def body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def record(self, body: bytes) -> None:
        self.owner.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "cookie": self.headers.get("Cookie", ""),
                "auth": self.headers.get("Authorization", ""),
                "body": body.decode("utf-8", "replace"),
            }
        )

    def send(self, status: int, payload: bytes, ctype: str, cookies: tuple[str, ...] = ()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(payload)

    def json(self, status: int, data: object, cookies: tuple[str, ...] = ()) -> None:
        self.send(status, json.dumps(data).encode(), "application/json; charset=utf-8", cookies)


# ----------------------------------------------------------------------------- fake PwnDoc
def audit_doc() -> dict:
    def field(cid: str, label: str, ftype: str, text: str, options: tuple[str, ...] = ()) -> dict:
        custom = {"_id": cid, "label": label, "fieldType": ftype}
        if options:
            custom["options"] = [{"locale": "en", "value": o} for o in options]
        return {"customField": custom, "text": text}

    finding = {
        "_id": FINDING,
        "title": "xss in search",
        "description": "<p>old description</p>",
        "observation": "",
        "poc": f"<p>open the page</p>{IMG}",
        "scope": "",
        "remediation": "",
        "references": ["https://owasp.org/www-community/attacks/xss/"],
        "customFields": [
            field("c1", "CWE", "input", ""),
            field("c2", "Severity", "select", "", ("Critical", "High", "Medium", "Low")),
            field("c3", "Vulnerable URL", "input", SECRET_URL),
        ],
    }
    return {"_id": AUDIT, "name": "Demo audit", "findings": [finding]}


INDEX = (
    "<!doctype html><html><head><title>fake pwndoc</title></head>"
    "<body><div id='app'>PwnDoc</div></body></html>"
)


class FakePwnDoc(Base):
    def user(self) -> tuple[str, str] | None:
        match = re.search(r"(?:^|;\s*)token=([^;]*)", self.headers.get("Cookie", ""))
        return BY_TOKEN.get(match.group(1)) if match else None

    def unauthorized(self) -> None:
        self.json(401, {"status": "error", "datas": {"msg": "Unauthorized"}})

    def do_GET(self) -> None:  # noqa: N802
        self.record(b"")
        path = self.path.split("?")[0]
        if path == "/api/users/me":
            user = self.user()
            return (
                self.json(200, {"status": "success", "datas": me_payload(*user)})
                if user
                else self.unauthorized()
            )
        if path.startswith("/api/audits/"):
            return (
                self.json(200, {"status": "success", "datas": audit_doc()})
                if self.user()
                else self.unauthorized()
            )
        if path.startswith("/api/"):
            return self.json(404, {"status": "error", "datas": {"msg": "not found"}})
        if path.startswith("/socket.io/"):
            return self.send(200, b'0{"sid":"fake-sid","upgrades":[]}', "text/plain; charset=UTF-8")
        if path == "/assets/blob.bin":
            return self.send(200, BINARY_ASSET, "application/octet-stream")
        self.send(200, INDEX.encode(), "text/html; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        raw = self.body()
        self.record(raw)
        if self.path != "/api/users/token":
            return self.json(404, {"status": "error", "datas": {"msg": "not found"}})
        creds = json.loads(raw or b"{}")
        entry = USERS.get(creds.get("username", ""))
        if not entry or entry[0] != creds.get("password"):
            return self.json(401, {"status": "error", "datas": {"msg": "Invalid credentials"}})
        flags = "HttpOnly; Secure; SameSite=Strict"
        self.json(
            200,
            {"status": "success", "datas": {"token": entry[2]}},
            (
                f"token={entry[2]}; Path=/; {flags}",
                f"refreshToken=r-{creds['username']}; Path=/api/users/refreshtoken; {flags}",
            ),
        )

    def do_PUT(self) -> None:  # noqa: N802
        raw = self.body()
        self.record(raw)
        if not self.user():
            return self.unauthorized()
        self.json(200, {"status": "success", "datas": "Audit Finding updated successfully"})


# ----------------------------------------------------------------------------- fake LLM
LLM_ARGS = {
    "title": "Reflected XSS in search",
    "description": '<p onclick="steal()">Refined description</p><script>window.__pwn=1</script>',
    "observation": "The search parameter is reflected unencoded.",
    "poc": "<ol><li>Open the search page.</li><li>Submit the payload.</li></ol>",  # dropped <img>
    "scope": "<p>/search endpoint</p>",
    "remediation": "<p>Encode output contextually.</p>",
    "references": ["https://cwe.mitre.org/data/definitions/79.html"],
    "custom_fields": [
        {"label": "CWE", "value": "CWE-79"},
        {"label": "Severity", "value": "high"},
        {"label": "Vulnerable URL", "value": "https://evil.example/pwn"},
    ],
}


class FakeLlm(Base):
    def do_GET(self) -> None:  # noqa: N802
        self.record(b"")
        if self.path.rstrip("/") == "/v1/models":
            models = [{"id": "fake-model", "object": "model", "created": 0, "owned_by": "test"}]
            return self.json(200, {"object": "list", "data": models})
        self.json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        raw = self.body()
        self.record(raw)
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self.json(404, {"error": {"message": "not found"}})
        request = json.loads(raw)
        usage = {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200}
        if request.get("tools"):
            call = {
                "id": "call_1",
                "type": "function",
                "function": {"name": "submit_refined_finding", "arguments": json.dumps(LLM_ARGS)},
            }
            message = {"role": "assistant", "content": None, "tool_calls": [call]}
            reason = "tool_calls"
        else:
            message, reason = {"role": "assistant", "content": "ok"}, "stop"
        self.json(
            200,
            {
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": request.get("model", "fake-model"),
                "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                "usage": usage,
            },
        )


# ----------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def upstream(tmp_path_factory):
    from pwndoc_ai.tls import TlsUnavailable, ensure_self_signed

    try:
        cert, key = ensure_self_signed(tmp_path_factory.mktemp("upstream_tls"))
    except TlsUnavailable:
        pytest.skip("cryptography not installed (needed to build the fake HTTPS PwnDoc)")
    server = Server(FakePwnDoc, tls=(cert, key))
    yield server
    server.close()


@pytest.fixture(scope="module")
def llm():
    server = Server(FakeLlm)
    yield server
    server.close()


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("gateway_data")


@pytest.fixture(scope="module")
def gateway(upstream, data_dir, tmp_path_factory):
    (data_dir / "config.json").write_text(
        json.dumps(
            {
                "pwndoc_url": upstream.url,
                "verify_tls": False,
                "use_web_search": False,
                "redact_field_labels": ["Vulnerable URL"],
            }
        )
    )
    port = free_port()
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("LLM_", "PWNDOC_", "OPENAI_", "HTTP_PROXY", "HTTPS_PROXY"))
    }
    env.update(
        PYTHONPATH=str(SRC),
        PYTHONUNBUFFERED="1",
        PWNDOC_AI_DATA_DIR=str(data_dir),
        NO_PROXY="127.0.0.1,localhost",
    )
    log_path = tmp_path_factory.mktemp("logs") / "gateway.log"
    code = (
        "import sys; from pwndoc_ai.server import run; "
        "run('127.0.0.1', int(sys.argv[1]), sys.argv[2])"
    )
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", code, str(port), str(data_dir)],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    pytest.fail(f"gateway exited early:\n{log_path.read_text()}")
                try:
                    if (
                        httpx.get(f"{base}/__ai/api/status", trust_env=False, timeout=1).status_code
                        == 200
                    ):
                        break
                except httpx.HTTPError:
                    time.sleep(0.2)
            else:
                pytest.fail(f"gateway did not start:\n{log_path.read_text()}")
            yield base
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
            print(log_path.read_text()[-3000:])


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        instance = pw.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        yield instance
        instance.close()


def http_login(base: str, username: str) -> tuple[httpx.Response, str]:
    """Log in through the gateway with a plain HTTP client; returns (response, Cookie header)."""
    resp = httpx.post(
        f"{base}/api/users/token",
        json={"username": username, "password": USERS[username][0], "totpToken": ""},
        trust_env=False,
    )
    cookies = "; ".join(c.split(";")[0] for c in resp.headers.get_list("set-cookie"))
    return resp, cookies


def browser_login(page, base: str, username: str) -> None:
    page.goto(f"{base}/login")
    status = page.evaluate(
        """async ([u, p]) => (await fetch('/api/users/token', {method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({username: u, password: p, totpToken: ''})})).status""",
        [username, USERS[username][0]],
    )
    assert status == 200


def open_finding_page(page, base: str) -> None:
    page.goto(f"{base}/login")
    page.evaluate(f"history.pushState({{}}, '', '/audits/{AUDIT}/findings/{FINDING}')")


# ----------------------------------------------------------------------------- HTTP-level tests
def test_status_is_open_but_unauthenticated(gateway):
    body = httpx.get(f"{gateway}/__ai/api/status", trust_env=False).json()
    assert body["authenticated"] is False and body["is_admin"] is False


def test_html_gets_injected_script_and_assets_pass_through(gateway, upstream):
    html = httpx.get(
        f"{gateway}/audits/{AUDIT}/findings/{FINDING}",
        headers={"Accept": "text/html"},
        trust_env=False,
    )
    assert html.status_code == 200
    assert "/__ai/static/inject.js" in html.text and html.text.index("inject.js") < html.text.index(
        "</body>"
    )
    assert "PwnDoc" in html.text

    blob = httpx.get(f"{gateway}/assets/blob.bin", trust_env=False)
    assert blob.content == BINARY_ASSET
    assert blob.headers["content-type"] == "application/octet-stream"
    assert "inject.js" not in blob.text

    poll = httpx.get(f"{gateway}/socket.io/?EIO=4&transport=polling", trust_env=False)
    assert poll.status_code == 200 and "fake-sid" in poll.text

    static = httpx.get(f"{gateway}/__ai/static/inject.js", trust_env=False)
    assert static.status_code == 200 and "javascript" in static.headers["content-type"]


def test_login_cookies_lose_secure_flag_over_plain_http(gateway):
    resp, _ = http_login(gateway, "admin")
    assert resp.status_code == 200
    cookies = resp.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert all("secure" not in c.lower() for c in cookies), cookies
    assert all("httponly" in c.lower() for c in cookies), cookies
    assert any(c.startswith("token=JWT%20admin-jwt") for c in cookies), cookies


def test_mutations_require_csrf_header(gateway):
    _, cookie = http_login(gateway, "admin")
    payload = {"audit_id": AUDIT, "finding_ids": [FINDING]}
    denied = httpx.post(
        f"{gateway}/__ai/api/jobs", json=payload, headers={"Cookie": cookie}, trust_env=False
    )
    assert denied.status_code == 403, denied.text
    assert "X-Requested-With" in denied.json()["detail"]


def test_plain_user_cannot_read_or_change_settings(gateway):
    _, cookie = http_login(gateway, "pentester")
    headers = {"Cookie": cookie, "X-Requested-With": "pwndoc-ai"}
    assert (
        httpx.get(f"{gateway}/__ai/api/settings", headers=headers, trust_env=False).status_code
        == 403
    )
    put = httpx.put(
        f"{gateway}/__ai/api/settings", json={"llm_model": "x"}, headers=headers, trust_env=False
    )
    assert put.status_code == 403
    status = httpx.get(f"{gateway}/__ai/api/status", headers=headers, trust_env=False).json()
    assert (
        status["authenticated"]
        and status["is_admin"] is False
        and status["username"] == "pentester"
    )
    anonymous = httpx.get(f"{gateway}/__ai/api/settings", trust_env=False)
    assert anonymous.status_code == 401


def test_upstream_receives_unmangled_token_cookie(gateway, upstream):
    _, cookie = http_login(gateway, "admin")
    httpx.get(f"{gateway}/__ai/api/status", headers={"Cookie": cookie}, trust_env=False)
    me_calls = upstream.calls("GET", "/api/users/me")
    assert me_calls and "token=JWT%20admin-jwt" in me_calls[-1]["cookie"]


# ----------------------------------------------------------------------------- browser tests
def test_plain_user_sees_update_button_but_no_settings(gateway, browser):
    page = browser.new_context().new_page()
    browser_login(page, gateway, "pentester")
    open_finding_page(page, gateway)
    expect(page.get_by_role("button", name="Update with AI")).to_be_visible()
    expect(page.get_by_role("button", name="AI Settings")).to_have_count(0)
    page.evaluate("history.pushState({}, '', '/settings')")
    page.wait_for_timeout(800)
    expect(page.get_by_role("button", name="AI Settings")).to_have_count(0)
    page.context.close()


def test_admin_end_to_end_update_flow(gateway, browser, upstream, llm, data_dir):
    context = browser.new_context()
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    browser_login(page, gateway, "admin")
    stored = {c["name"]: c for c in context.cookies()}
    assert "token" in stored, "token cookie was not stored by the browser"
    assert stored["token"]["secure"] is False, "gateway left Secure on the cookie over HTTP"
    assert stored["token"]["httpOnly"] is True

    open_finding_page(page, gateway)
    expect(page.locator("#pwndoc-ai-host .ai-dock")).to_be_visible()
    expect(page.get_by_role("button", name="Update with AI")).to_be_visible()

    # --- AI Settings: configure the fake LLM ---
    page.get_by_role("button", name="AI Settings").click()
    page.get_by_label("Base URL").fill(f"{llm.url}/v1")
    page.get_by_label("API key").fill("sk-test-key")
    page.get_by_label("Model", exact=True).fill("fake-model")
    page.get_by_role("button", name="Test connection").click()
    expect(page.locator(".ai-note.ai-ok")).to_contain_text("ok", timeout=10000)
    page.get_by_role("button", name="Fetch models").click()
    expect(page.get_by_text("1 models found")).to_be_visible(timeout=10000)
    saved = json.loads((data_dir / "config.json").read_text())
    assert saved["llm_model"] == "fake-model" and saved["llm_api_key"] == "sk-test-key"
    assert oct(os.stat(data_dir / "config.json").st_mode & 0o777) == "0o600"
    page.keyboard.press("Escape")

    # --- Update with AI: preview, diff, apply ---
    page.get_by_role("button", name="Update with AI").click()
    drawer = page.get_by_role("dialog")
    expect(drawer.get_by_text("This finding")).to_be_visible()
    drawer.get_by_role("button", name="Start").click()
    expect(drawer.get_by_text("Ready to review").first).to_be_visible(timeout=20000)
    assert upstream.calls("PUT", "/api/audits/") == [], "preview mode must not write to PwnDoc"

    drawer.get_by_role("button", name="Show changes for", exact=False).first.click()
    expect(drawer.locator(".ai-after pre").first).to_be_visible()
    diff_text = drawer.inner_text()
    assert "Refined description" in diff_text and "Reflected XSS in search" in diff_text
    assert page.evaluate("window.__pwn") is None, "script from model output executed"

    drawer.get_by_role("button", name="Apply all").click()
    expect(drawer.get_by_role("button", name="Reload page")).to_be_visible(timeout=15000)

    # --- what PwnDoc received ---
    puts = upstream.calls("PUT", f"/api/audits/{AUDIT}/findings/{FINDING}")
    assert len(puts) == 1, [p["path"] for p in upstream.calls("PUT", "/")]
    assert "token=JWT%20admin-jwt" in puts[0]["cookie"], "apply must act as the signed-in user"
    body = json.loads(puts[0]["body"])
    assert "_id" not in body
    assert body["title"] == "Reflected XSS in search"
    assert "<script" not in body["description"] and "onclick" not in body["description"]
    assert "Refined description" in body["description"]
    assert IMG in body["poc"], "evidence screenshot was lost"
    assert "<li>Open the search page.</li>" in body["poc"]
    fields = {e["customField"]["label"]: e for e in body["customFields"]}
    assert fields["Vulnerable URL"]["text"] == SECRET_URL, "redacted field was modified"
    assert fields["CWE"]["text"] == "CWE-79"
    assert fields["Severity"]["text"] == "High"
    assert fields["CWE"]["customField"]["_id"] == "c1"
    assert body["references"][0] == "https://owasp.org/www-community/attacks/xss/"
    assert "https://cwe.mitre.org/data/definitions/79.html" in body["references"]

    # --- what the LLM saw ---
    chat = llm.calls("POST", "/v1/chat/completions")
    assert chat, "LLM was never called"
    assert all(SECRET_URL not in c["body"] and "secret.internal" not in c["body"] for c in chat)
    assert all(c["auth"] == "Bearer sk-test-key" for c in chat)
    refine_request = json.loads(next(c["body"] for c in chat if '"tools"' in c["body"]))
    assert "xss in search" in json.dumps(refine_request["messages"])

    # --- backup of the originals ---
    backups = sorted((data_dir / "backups" / AUDIT).glob("*.json"))
    assert len(backups) == 1, backups
    original = json.loads(backups[0].read_text())
    assert SECRET_URL in json.dumps(original) and "old description" in json.dumps(original)

    assert errors == [], errors
    context.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-x"]))
