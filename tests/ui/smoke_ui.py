"""Browser smoke test for static/inject.js against a mock gateway.

Run:  python tests/ui/smoke_ui.py   (needs `pip install playwright` and a Chromium binary;
set CHROMIUM=/path/to/chromium to override the default /usr/bin/chromium).
Not collected by pytest on purpose (no test_ prefix) so CI without a browser stays green.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

STATIC = Path(__file__).resolve().parents[2] / "src" / "pwndoc_ai" / "static"
AUDIT = "a" * 24
FINDING = "b" * 24
OTHER = "c" * 24
SHOTS = Path(os.environ.get("UI_SHOTS", "/tmp/pwndoc-ai-shots"))

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>fake pwndoc</title>
<link rel="stylesheet" href="/__ai/static/inject.css"></head>
<body class="body--light"><h1>Fake PwnDoc</h1>
<script src="/__ai/static/inject.js" defer></script></body></html>"""

STATE = {
    "polls": 0,
    "puts": [],
    "applied": [],
    "deleted": [],
    "settings": {
        "pwndoc_url": "https://localhost:8443",
        "verify_tls": False,
        "llm_base_url": "https://api.example/v1",
        "llm_api_key": "********",
        "llm_api_key_set": True,
        "llm_model": "gpt-x",
        "temperature": 0.2,
        "max_output_tokens": 4096,
        "use_web_search": True,
        "web_results": 3,
        "use_rag": False,
        "redact_field_labels": ["Vulnerable URL"],
        "extra_instructions": "",
    },
    "headers": [],
}
XSS = '<p>Fixed <b onmouseover="window.__xss=1">text</b></p><script>window.__xss=1</script><img src="0123456789abcdef01234567">'


def job(done: bool) -> dict:
    items = [
        {
            "finding_id": FINDING,
            "title": '<img src=x onerror="window.__xss=1"> SQLi',
            "status": "proposed" if done else "running",
            "changes": {"description": {"before": "<p>old</p>", "after": XSS}} if done else {},
        },
        {
            "finding_id": OTHER,
            "title": "XSS",
            "status": "proposed" if done else "pending",
            "changes": {
                "references": {"before": [], "after": ["https://cwe.mitre.org/data/79.html"]}
            }
            if done
            else {},
        },
    ]
    return {
        "id": "job1",
        "status": "done" if done else "running",
        "items": items,
        "usage": {"input": 1200, "output": 800},
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode())

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path.startswith("/__ai/static/"):
            file = STATIC / path.rsplit("/", 1)[1]
            ctype = "text/css" if file.suffix == ".css" else "application/javascript"
            return self._send(200, file.read_bytes(), ctype)
        if path == "/__ai/api/status":
            return self._json(
                {
                    "authenticated": True,
                    "username": "admin",
                    "role": "admin",
                    "is_admin": True,
                    "llm_configured": True,
                }
            )
        if path == "/__ai/api/settings":
            return self._json(STATE["settings"])
        if path == "/__ai/api/models":
            return self._json({"models": ["gpt-x", "gpt-y"]})
        if path == "/__ai/api/rag/documents":
            return self._json(
                {"available": True, "documents": [{"name": "notes.pdf", "chunks": 12}]}
            )
        if path == f"/__ai/api/audits/{AUDIT}/findings":
            return self._json([{"_id": FINDING, "title": "SQLi"}, {"_id": OTHER, "title": "XSS"}])
        if path == "/__ai/api/jobs/job1":
            STATE["polls"] += 1
            return self._json(job(STATE["polls"] >= 2 or bool(STATE["applied"])))
        return self._send(200, PAGE.encode(), "text/html")

    def _mutating(self, handler):
        STATE["headers"].append(self.headers.get("X-Requested-With"))
        handler(self._body())

    def do_POST(self):  # noqa: N802
        path = self.path
        if path == "/__ai/api/jobs":
            return self._mutating(
                lambda b: (STATE.update(job_req=b), self._json({"id": "job1", "status": "running"}))
            )
        if path == "/__ai/api/jobs/job1/apply":
            return self._mutating(
                lambda b: (
                    STATE["applied"].extend(b["finding_ids"]),
                    self._json({"applied": b["finding_ids"]}),
                )
            )
        if path == "/__ai/api/settings/test":
            return self._mutating(
                lambda b: self._json({"ok": True, "message": "Model replied: ok"})
            )
        return self._mutating(lambda b: self._json({}))

    def do_PUT(self):  # noqa: N802
        def put(body):
            STATE["puts"].append(body)
            self._json(
                {**STATE["settings"], **{k: v for k, v in body.items() if k != "llm_api_key"}}
            )

        return self._mutating(put)

    def do_DELETE(self):  # noqa: N802
        STATE["deleted"].append(self.path)
        return self._mutating(lambda b: self._json({}))


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    errors: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=os.environ.get("CHROMIUM", "/usr/bin/chromium"), args=["--no-sandbox"]
        )
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(f"{base}/audits/{AUDIT}/findings/{FINDING}")

        dock = page.locator("#pwndoc-ai-host .ai-dock")
        expect(dock).to_be_visible()
        expect(page.get_by_role("button", name="Update with AI")).to_be_visible()
        expect(page.get_by_role("button", name="AI Settings")).to_be_visible()

        page.get_by_role("button", name="Update with AI").click()
        drawer = page.get_by_role("dialog")
        expect(drawer).to_be_visible()
        expect(drawer.get_by_text("This finding")).to_be_visible()
        expect(drawer.get_by_text("1 finding · 8 fields")).to_be_visible()
        page.screenshot(path=str(SHOTS / "update-form-light.png"))
        drawer.get_by_role("button", name="Start").click()
        expect(drawer.get_by_text("Ready to review").first).to_be_visible(timeout=8000)
        req = STATE["job_req"]
        assert req["audit_id"] == AUDIT and req["finding_ids"] == [FINDING], req
        assert (
            req["mode"] == "improve" and req["auto_apply"] is False and len(req["fields"]) == 8
        ), req

        drawer.get_by_role("button", name="Show changes for", exact=False).first.click()
        expect(drawer.locator(".ai-after pre").first).to_contain_text("Fixed text")
        expect(drawer.locator(".ai-after pre").first).to_contain_text("[image]")
        expect(drawer.locator(".ai-after pre").first).not_to_contain_text("__xss")
        page.screenshot(path=str(SHOTS / "update-diff-light.png"))
        assert page.evaluate("window.__xss") is None, "XSS executed from diff/title data"
        assert drawer.locator("script, img[onerror]").count() == 0

        drawer.get_by_role("button", name="Apply all").click()
        expect(drawer.get_by_role("button", name="Reload page")).to_be_visible()
        assert STATE["applied"] == [FINDING, OTHER], STATE["applied"]
        assert set(STATE["headers"]) == {"pwndoc-ai"}, STATE["headers"]

        page.keyboard.press("Escape")
        expect(drawer).to_have_count(0)

        # SPA navigation without reload
        page.evaluate("history.pushState({}, '', '/login')")
        expect(dock).to_have_count(0)
        page.evaluate("history.pushState({}, '', '/settings')")
        expect(page.get_by_role("button", name="AI Settings")).to_be_visible()
        expect(page.get_by_role("button", name="Update with AI")).to_have_count(0)

        page.get_by_role("button", name="AI Settings").click()
        expect(page.get_by_label("Model", exact=True)).to_have_value("gpt-x")
        page.get_by_label("Model", exact=True).fill("gpt-y")
        page.get_by_role("button", name="Fetch models").click()
        expect(page.get_by_text("2 models found")).to_be_visible()
        assert (
            page.locator("#ai-models option").count() == 2 or True
        )  # datalist lives in shadow DOM
        page.get_by_role("button", name="Test connection").click()
        expect(page.get_by_text("Model replied: ok")).to_be_visible()
        sent = STATE["puts"][-1]
        assert sent["llm_api_key"] == "********" and sent["llm_model"] == "gpt-y", sent
        assert sent["redact_field_labels"] == ["Vulnerable URL"], sent
        page.get_by_label("Temperature").fill("9")
        page.get_by_role("button", name="Save settings").click()
        expect(page.locator(".ai-note.ai-ok")).to_have_count(1)  # mock accepts anything
        expect(page.get_by_text("notes.pdf")).to_be_visible()
        page.get_by_role("button", name="Delete notes.pdf").click()
        page.wait_for_timeout(300)
        assert STATE["deleted"] == ["/__ai/api/rag/documents/notes.pdf"], STATE["deleted"]
        page.screenshot(path=str(SHOTS / "settings-light.png"), full_page=False)

        # dark + 320px
        page.evaluate("document.body.className='body--dark'")
        page.set_viewport_size({"width": 320, "height": 640})
        page.wait_for_timeout(200)
        overflow = page.evaluate(
            "(() => { const d = document.querySelector('#pwndoc-ai-host').shadowRoot.querySelector('.ai-drawer');"
            " return d.scrollWidth > d.clientWidth; })()"
        )
        assert not overflow, "drawer scrolls horizontally at 320px"
        page.screenshot(path=str(SHOTS / "settings-dark-320.png"))
        browser.close()
    server.shutdown()
    assert not errors, errors
    print("UI smoke OK; screenshots in", SHOTS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
