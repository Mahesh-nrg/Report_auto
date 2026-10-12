import dataclasses
import json
import sys
import time
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from pwndoc_ai import gateway as server_gateway
from pwndoc_ai import server
from pwndoc_ai.llm import LlmError, Usage
from pwndoc_ai.server import create_app, raw_cookie

CSRF = {"X-Requested-With": "pwndoc-ai"}
ADMIN = {"cookie": "token=JWT%20admin", **CSRF}
USER = {"cookie": "token=JWT%20user", **CSRF}
OTHER = {"cookie": "token=JWT%20other", **CSRF}
AUDIT = "a1b2c3"
FINDINGS = [
    {
        "_id": "f1",
        "title": "XSS",
        "description": "old",
        "customFields": [
            {"customField": {"label": "Severity"}, "text": "High"},
            {"customField": {"label": "CWE"}, "text": "CWE-79"},
        ],
    },
    {"_id": "f2", "title": "SQLi", "description": "old2"},
]
puts: list[tuple[str, dict]] = []


@pytest.fixture(scope="module", autouse=True)
def refiner_api():
    try:
        import pwndoc_ai.refiner as module

        yield module
        return
    except ImportError:
        pass
    stub = types.ModuleType("pwndoc_ai.refiner")

    @dataclasses.dataclass(frozen=True)
    class RefineOptions:
        fields: tuple = ("description",)
        mode: str = "improve"

    @dataclasses.dataclass(frozen=True)
    class Proposal:
        finding_id: str
        title: str
        payload: dict
        changes: dict
        usage: Usage

    stub.RefineOptions, stub.Proposal = RefineOptions, Proposal
    stub.finding_update_payload = lambda f: {k: v for k, v in f.items() if k != "_id"}
    sys.modules["pwndoc_ai.refiner"] = stub
    yield stub
    del sys.modules["pwndoc_ai.refiner"]


ROLES = {
    "JWT%20admin": ("alice", "admin"),
    "JWT%20user": ("bob", "user"),
    "JWT%20other": ("eve", "user"),
}


def upstream(request: httpx.Request) -> httpx.Response:
    cookie = request.headers.get("cookie", "")
    role = next((v for k, v in ROLES.items() if f"token={k}" in cookie), None)
    if role is None:
        return httpx.Response(401, json={"status": "error", "datas": "Invalid token"})
    path = request.url.path
    if path == "/api/users/me":
        return httpx.Response(
            200, json={"status": "success", "datas": {"username": role[0], "role": role[1]}}
        )
    if path == f"/api/audits/{AUDIT}":
        return httpx.Response(200, json={"status": "success", "datas": {"findings": FINDINGS}})
    if path.startswith(f"/api/audits/{AUDIT}/findings/") and request.method == "PUT":
        puts.append((path.rsplit("/", 1)[1], json.loads(request.content)))
        return httpx.Response(200, json={"status": "success", "datas": "Finding updated"})
    return httpx.Response(404, json={"status": "error", "datas": {"msg": "Audit not found"}})


class FakeRefiner:
    def __init__(self, api):
        self.api = api

    def refine(self, finding, options):
        if finding["title"] == "SQLi":
            raise LlmError("bad model output")
        return self.api.Proposal(
            finding_id=finding["_id"],
            title=finding["title"],
            payload={"description": "<p>better</p>"},
            changes={"description": {"before": "old", "after": "better"}},
            usage=Usage(7, 3),
        )


@pytest.fixture
def client(tmp_path, refiner_api):
    puts.clear()
    app = create_app(
        tmp_path,
        transport=httpx.MockTransport(upstream),
        refiner_factory=lambda settings: FakeRefiner(refiner_api),
        env={},
    )
    with TestClient(app) as test_client:
        yield test_client


def wait_done(client, job_id, headers):
    for _ in range(100):
        view = client.get(f"/__ai/api/jobs/{job_id}", headers=headers).json()
        if view["status"] not in ("queued", "running"):
            return view
        time.sleep(0.05)
    raise AssertionError("job did not finish")


# -- auth / csrf ---------------------------------------------------------------------------------
def test_status_unauthenticated_is_ok(client):
    body = client.get("/__ai/api/status").json()
    assert body["authenticated"] is False and body["is_admin"] is False
    assert body["llm_configured"] is False and "version" in body


def test_status_reports_identity_from_upstream_me(client):
    body = client.get("/__ai/api/status", headers=ADMIN).json()
    assert (body["authenticated"], body["username"], body["role"], body["is_admin"]) == (
        True,
        "alice",
        "admin",
        True,
    )


def test_invalid_token_is_unauthenticated(client):
    body = client.get("/__ai/api/status", headers={"cookie": "token=JWT%20bogus"}).json()
    assert body["authenticated"] is False


def test_raw_cookie_is_not_unquoted():
    assert raw_cookie("a=1; token=JWT%20abc; b=2", "token") == "JWT%20abc"
    assert raw_cookie("xtoken=1", "token") is None


def test_identity_cache_avoids_repeat_me_calls(tmp_path):
    calls = []

    def counting(request):
        calls.append(request.url.path)
        return upstream(request)

    app = create_app(tmp_path, transport=httpx.MockTransport(counting), env={})
    with TestClient(app) as c:
        for _ in range(3):
            c.get("/__ai/api/status", headers=ADMIN)
    assert calls.count("/api/users/me") == 1


def test_settings_requires_admin(client):
    assert client.get("/__ai/api/settings").status_code == 401
    assert client.get("/__ai/api/settings", headers=USER).status_code == 403
    assert client.get("/__ai/api/settings", headers=ADMIN).status_code == 200


def test_mutations_require_csrf_header(client):
    headers = {"cookie": "token=JWT%20admin"}
    response = client.put("/__ai/api/settings", json={"llm_model": "m"}, headers=headers)
    assert response.status_code == 403
    assert "X-Requested-With" in response.json()["detail"]


def test_settings_roundtrip_masks_key_and_validates(client):
    put = client.put(
        "/__ai/api/settings", json={"llm_api_key": "sk-1", "llm_model": "m1"}, headers=ADMIN
    )
    assert put.status_code == 200
    assert put.json()["llm_api_key"] == "********" and "sk-1" not in put.text
    got = client.get("/__ai/api/settings", headers=ADMIN).json()
    assert got["llm_model"] == "m1" and got["llm_api_key_set"] is True
    bad = client.put("/__ai/api/settings", json={"temperature": 99}, headers=ADMIN)
    assert bad.status_code == 400 and "temperature" in bad.json()["detail"]
    assert client.get("/__ai/api/status").json()["llm_configured"] is True


def test_non_admin_cannot_put_settings(client):
    assert (
        client.put("/__ai/api/settings", json={"llm_model": "x"}, headers=USER).status_code == 403
    )


def test_llm_test_and_models_use_current_settings(client, monkeypatch):
    class FakeLlm:
        def __init__(self, settings):
            self.model = settings.llm_model

        def ping(self):
            if self.model == "bad":
                raise LlmError("401 unauthorized")
            return "ok"

        def list_models(self):
            return ["m-a", "m-b"]

    monkeypatch.setattr(server, "LlmClient", FakeLlm)
    assert client.post("/__ai/api/settings/test", headers=ADMIN).json() == {
        "ok": True,
        "reply": "ok",
    }
    client.put("/__ai/api/settings", json={"llm_model": "bad"}, headers=ADMIN)
    result = client.post("/__ai/api/settings/test", headers=ADMIN).json()
    assert result["ok"] is False and "401" in result["error"]
    assert client.get("/__ai/api/models", headers=ADMIN).json() == {"models": ["m-a", "m-b"]}
    assert client.get("/__ai/api/models", headers=USER).status_code == 403


# -- static ----------------------------------------------------------------------------------
def test_static_assets_served_no_store(client, tmp_path, monkeypatch):
    (tmp_path / "inject.js").write_text("// js")
    monkeypatch.setattr(server, "STATIC_DIR", tmp_path)
    response = client.get("/__ai/static/inject.js")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("text/javascript")
    assert client.get("/__ai/static/inject.css").status_code == 404  # file absent
    assert client.get("/__ai/static/secret.txt").status_code == 404  # not allow-listed


# -- rag -------------------------------------------------------------------------------------
class FakeIndex:
    def __init__(self, base):
        self.docs_dir = base
        self.removed = []

    def add_file(self, path):
        if path.read_bytes() == b"corrupt":
            raise ValueError("unreadable")
        return 3

    def remove(self, name):
        self.removed.append(name)

    def list_documents(self):
        return [{"name": "n.md", "chunks": 3}]


def test_rag_unavailable_reports_cleanly(client):
    client.app.state.gateway.rag_index = lambda: None
    assert client.get("/__ai/api/rag/documents", headers=ADMIN).json() == {
        "available": False,
        "documents": [],
    }
    files = {"file": ("n.md", b"hi")}
    assert client.post("/__ai/api/rag/documents", files=files, headers=ADMIN).status_code == 400


def test_rag_upload_list_delete(client, tmp_path):
    index = FakeIndex(tmp_path)
    client.app.state.gateway.rag_index = lambda: index
    ok = client.post(
        "/__ai/api/rag/documents", files={"file": ("../../evil name.md", b"hello")}, headers=ADMIN
    )
    assert ok.status_code == 200 and ok.json() == {"name": "evil name.md", "chunks": 3}
    assert (tmp_path / "evil name.md").read_bytes() == b"hello"
    assert (
        client.get("/__ai/api/rag/documents", headers=ADMIN).json()["documents"][0]["name"]
        == "n.md"
    )
    assert client.delete("/__ai/api/rag/documents/..%2Fx.md", headers=ADMIN).status_code in (
        200,
        404,
    )
    assert client.delete("/__ai/api/rag/documents/n.md", headers=ADMIN).json() == {
        "deleted": "n.md"
    }
    assert index.removed[-1] == "n.md"


def test_rag_upload_rejections(client, tmp_path, monkeypatch):
    client.app.state.gateway.rag_index = lambda: FakeIndex(tmp_path)
    assert (
        client.post(
            "/__ai/api/rag/documents", files={"file": ("a.exe", b"x")}, headers=ADMIN
        ).status_code
        == 400
    )
    bad = client.post(
        "/__ai/api/rag/documents", files={"file": ("c.md", b"corrupt")}, headers=ADMIN
    )
    assert bad.status_code == 400 and not (tmp_path / "c.md").exists()
    monkeypatch.setattr(server, "MAX_UPLOAD_BYTES", 4)
    assert (
        client.post(
            "/__ai/api/rag/documents", files={"file": ("d.md", b"12345")}, headers=ADMIN
        ).status_code
        == 413
    )
    assert (
        client.post(
            "/__ai/api/rag/documents", files={"file": ("e.md", b"x")}, headers=USER
        ).status_code
        == 403
    )


# -- findings + jobs -------------------------------------------------------------------------
def test_findings_list_with_severity(client):
    body = client.get(f"/__ai/api/audits/{AUDIT}/findings", headers=USER).json()
    assert body["findings"] == [
        {"id": "f1", "title": "XSS", "severity": "High"},
        {"id": "f2", "title": "SQLi", "severity": ""},
    ]


def test_findings_errors_are_mapped(client):
    assert client.get("/__ai/api/audits/missing/findings", headers=USER).status_code == 404
    assert client.get(f"/__ai/api/audits/{AUDIT}/findings").status_code == 401
    assert client.get("/__ai/api/audits/bad$id/findings", headers=USER).status_code == 400


def test_job_lifecycle_propose_apply_backup(client, tmp_path):
    start = client.post(
        "/__ai/api/jobs",
        json={"audit_id": AUDIT, "finding_ids": None, "fields": ["description"]},
        headers=USER,
    )
    assert start.status_code == 200
    job_id = start.json()["job_id"]
    view = wait_done(client, job_id, USER)
    assert view["status"] == "done" and view["usage"] == {"input": 7, "output": 3}
    assert [i["status"] for i in view["items"]] == ["proposed", "failed"]
    assert view["items"][0]["changes"]["description"]["after"] == "better"
    assert puts == []  # nothing written yet

    applied = client.post(
        f"/__ai/api/jobs/{job_id}/apply", json={"finding_ids": ["f1"]}, headers=USER
    )
    assert applied.status_code == 200
    assert applied.json()["items"][0]["status"] == "applied"
    assert puts == [("f1", {"description": "<p>better</p>"})]
    backups = list((tmp_path / "backups" / AUDIT).glob("*.json"))
    assert len(backups) == 1 and len(json.loads(backups[0].read_text())) == 2


def test_job_auto_apply(client):
    job_id = client.post(
        "/__ai/api/jobs", json={"audit_id": AUDIT, "auto_apply": True}, headers=USER
    ).json()["job_id"]
    view = wait_done(client, job_id, USER)
    assert view["items"][0]["status"] == "applied"
    assert [p[0] for p in puts] == ["f1"]


def test_other_user_gets_404_for_job(client):
    job_id = client.post("/__ai/api/jobs", json={"audit_id": AUDIT}, headers=USER).json()["job_id"]
    wait_done(client, job_id, USER)
    for path, method in (
        (f"/__ai/api/jobs/{job_id}", "get"),
        (f"/__ai/api/jobs/{job_id}/cancel", "post"),
    ):
        assert getattr(client, method)(path, headers=OTHER).status_code == 404
    applied = client.post(
        f"/__ai/api/jobs/{job_id}/apply", json={"finding_ids": ["f1"]}, headers=OTHER
    )
    assert applied.status_code == 404 and puts == []


def test_job_validation_and_conflict(client, refiner_api):
    assert client.post("/__ai/api/jobs", json={"audit_id": "../x"}, headers=USER).status_code == 422
    assert (
        client.post(
            "/__ai/api/jobs", json={"audit_id": AUDIT, "fields": ["nope"]}, headers=USER
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/__ai/api/jobs", json={"audit_id": AUDIT, "mode": "weird"}, headers=USER
        ).status_code
        == 422
    )
    assert (
        client.post("/__ai/api/jobs", json={"audit_id": AUDIT}).status_code == 403
    )  # no csrf header
    assert (
        client.post("/__ai/api/jobs", json={"audit_id": AUDIT}, headers={**CSRF}).status_code == 401
    )


def test_apply_unknown_finding_is_400(client):
    job_id = client.post("/__ai/api/jobs", json={"audit_id": AUDIT}, headers=USER).json()["job_id"]
    wait_done(client, job_id, USER)
    response = client.post(
        f"/__ai/api/jobs/{job_id}/apply", json={"finding_ids": ["zzz"]}, headers=USER
    )
    assert response.status_code == 400


# -- tls -------------------------------------------------------------------------------------
def test_self_signed_certificate_is_created_once_with_private_key(tmp_path):
    import os
    import stat

    pytest.importorskip("cryptography")
    from cryptography import x509

    from pwndoc_ai.tls import ensure_self_signed

    cert, key = ensure_self_signed(tmp_path)
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
    loaded = x509.load_pem_x509_certificate(open(cert, "rb").read())
    names = loaded.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "localhost" in names.get_values_for_type(x509.DNSName)
    assert ensure_self_signed(tmp_path) == (cert, key)  # reused, not regenerated
    assert open(cert, "rb").read() == open(cert, "rb").read()


def test_https_without_cryptography_gives_clear_error(tmp_path, monkeypatch):
    import builtins

    from pwndoc_ai.tls import TlsUnavailable, ensure_self_signed

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(TlsUnavailable, match="pwndoc-ai\\[https\\]"):
        ensure_self_signed(tmp_path / "fresh")


# -- identity: roles shapes, upstream failures, caching --------------------------------------------
BOB = {"cookie": "token=JWT%20anyone", **CSRF}


def me_app(tmp_path, respond, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        return respond(request)

    return create_app(tmp_path, transport=httpx.MockTransport(handler), env={})


def me_json(datas, status=200):
    return lambda request: httpx.Response(status, json={"status": "success", "datas": datas})


@pytest.mark.parametrize(
    ("datas", "is_admin", "role", "roles"),
    [
        ({"username": "carol", "roles": ["user", "admin"]}, True, "admin", ["user", "admin"]),
        ({"username": "dave", "roles": ["user"]}, False, "user", ["user"]),
        ({"username": "erin", "roles": []}, False, "", []),
        ({"username": "old", "role": "admin"}, True, "admin", ["admin"]),  # pre-1.x shape
        ({"username": "old2", "role": "user"}, False, "user", ["user"]),
    ],
)
def test_both_pwndoc_me_shapes_decide_admin_correctly(tmp_path, datas, is_admin, role, roles):
    with TestClient(me_app(tmp_path, me_json(datas))) as c:
        body = c.get("/__ai/api/status", headers=BOB).json()
        assert (body["is_admin"], body["role"], body["roles"]) == (is_admin, role, roles)
        assert c.get("/__ai/api/settings", headers=BOB).status_code == (200 if is_admin else 403)


def test_status_reports_empty_roles_when_signed_out(client):
    body = client.get("/__ai/api/status").json()
    assert body["roles"] == [] and body["role"] == ""


@pytest.mark.parametrize("datas", [{"username": ""}, {"username": "  ", "roles": ["admin"]}, {}])
def test_identity_without_a_username_is_not_signed_in(tmp_path, datas):
    with TestClient(me_app(tmp_path, me_json(datas))) as c:
        assert c.get("/__ai/api/status", headers=BOB).json()["authenticated"] is False
        assert c.post("/__ai/api/jobs", headers=BOB, json={"audit_id": AUDIT}).status_code == 401


def test_upstream_down_is_502_not_a_misleading_401_and_leaks_no_internal_url(tmp_path):
    def down(request):
        raise httpx.ConnectError("refused to db-host.internal:27017")

    with TestClient(me_app(tmp_path, down)) as c:
        for response in (
            c.get("/__ai/api/status", headers=BOB),
            c.post("/__ai/api/jobs", headers=BOB, json={"audit_id": AUDIT}),
        ):
            assert response.status_code == 502
            assert "internal" not in response.text and "localhost:8443" not in response.text


def test_upstream_5xx_and_garbage_me_payloads_are_502(tmp_path):
    with TestClient(me_app(tmp_path, me_json({"msg": "boom"}, status=500))) as c:
        assert c.get("/__ai/api/status", headers=BOB).status_code == 502
    html = lambda request: httpx.Response(200, text="<html>not pwndoc</html>")  # noqa: E731
    with TestClient(me_app(tmp_path, html)) as c:
        assert c.get("/__ai/api/status", headers=BOB).status_code == 502


def test_upstream_failures_are_not_cached(tmp_path):
    state = {"up": False}

    def flaky(request):
        if not state["up"]:
            return httpx.Response(503, json={"status": "error", "datas": "down"})
        return httpx.Response(200, json={"status": "success", "datas": {"username": "u"}})

    with TestClient(me_app(tmp_path, flaky)) as c:
        assert c.get("/__ai/api/status", headers=BOB).status_code == 502
        state["up"] = True
        assert c.get("/__ai/api/status", headers=BOB).json()["authenticated"] is True


def test_rejected_tokens_are_negatively_cached_for_a_short_time(tmp_path):
    calls: list[str] = []
    app = me_app(
        tmp_path, lambda r: httpx.Response(401, json={"status": "error", "datas": "no"}), calls
    )
    with TestClient(app) as c:
        for _ in range(3):
            assert c.get("/__ai/api/status", headers=BOB).json()["authenticated"] is False
        assert calls == ["/api/users/me"]  # bogus cookies cannot hammer PwnDoc
        gw = app.state.gateway
        expiry, cached = gw._identities["JWT%20anyone"]
        assert cached is None and expiry - time.monotonic() <= server_gateway.NEGATIVE_TTL
        gw._identities["JWT%20anyone"] = (0.0, None)  # TTL elapsed
        c.get("/__ai/api/status", headers=BOB)
        assert len(calls) == 2


def test_identity_cache_is_bounded(tmp_path):
    app = me_app(tmp_path, me_json({"username": "u"}))
    gw = app.state.gateway
    now = time.monotonic()
    for i in range(server_gateway.IDENTITY_CACHE_MAX * 2 + 5):
        gw._remember(f"JWT%20bogus{i}", None, now)
    assert len(gw._identities) <= server_gateway.IDENTITY_CACHE_MAX + 1


# -- settings ---------------------------------------------------------------------------------
def test_locked_settings_cannot_be_changed_from_the_browser(client):
    current = client.get("/__ai/api/settings", headers=ADMIN).json()
    for name, value in (("pwndoc_url", "https://evil.example"), ("verify_tls", False)):
        response = client.put("/__ai/api/settings", headers=ADMIN, json={name: value})
        assert response.status_code == 400 and "CLI" in response.json()["detail"]
    # re-sending the current values (as the UI form does) is fine
    ok = client.put(
        "/__ai/api/settings",
        headers=ADMIN,
        json={
            "pwndoc_url": current["pwndoc_url"],
            "verify_tls": current["verify_tls"],
            "llm_model": "m",
        },
    )
    assert ok.status_code == 200 and ok.json()["llm_model"] == "m"


def test_unwritable_data_dir_is_a_clear_500_without_paths(client, monkeypatch):
    def boom(self, patch):
        raise PermissionError(13, "Permission denied", "/srv/secret/data/config.json")

    from pwndoc_ai.config import ConfigStore

    monkeypatch.setattr(ConfigStore, "update", boom)
    response = client.put("/__ai/api/settings", headers=ADMIN, json={"llm_model": "x"})
    assert response.status_code == 500
    assert "writable" in response.json()["detail"] and "/srv/secret" not in response.text


def test_oversized_finding_id_lists_are_rejected(client):
    ids = [f"f{i}" for i in range(501)]
    assert (
        client.post(
            "/__ai/api/jobs", headers=USER, json={"audit_id": AUDIT, "finding_ids": ids}
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/__ai/api/jobs/{'a' * 8}/apply", headers=USER, json={"finding_ids": ids}
        ).status_code
        == 422
    )


# -- generic error messages -------------------------------------------------------------------
def test_rag_index_failure_does_not_leak_internal_paths(client, tmp_path):
    class BrokenIndex:
        docs_dir = tmp_path / "docs"

        def add_file(self, path):
            raise RuntimeError("chroma exploded at /srv/secret/rag/chroma.sqlite3")

    BrokenIndex.docs_dir.mkdir()
    client.app.state.gateway.rag = BrokenIndex()
    response = client.post(
        "/__ai/api/rag/documents", headers=ADMIN, files={"file": ("notes.txt", b"hello")}
    )
    assert response.status_code == 400
    assert "/srv/secret" not in response.text and "notes.txt" in response.json()["detail"]
    assert not (BrokenIndex.docs_dir / "notes.txt").exists()  # the rejected upload is removed


def test_apply_failure_is_generic(client, monkeypatch):
    from types import SimpleNamespace

    from pwndoc_ai.jobs import JobError

    jobs = client.app.state.gateway.jobs

    def refuse(job, cookies, ids):
        raise JobError("could not write backup to /srv/secret/data/backups/a1/x.json")

    monkeypatch.setattr(jobs, "get", lambda job_id, owner: SimpleNamespace(view=lambda: {}))
    monkeypatch.setattr(jobs, "apply", refuse)
    response = client.post("/__ai/api/jobs/j1/apply", headers=USER, json={"finding_ids": ["f1"]})
    assert response.status_code == 500
    assert "/srv/secret" not in response.text and "gateway log" in response.json()["detail"]


def test_pwndoc_server_errors_are_generic_but_auth_errors_pass_through(tmp_path):
    def respond(request):
        if request.url.path == "/api/users/me":
            return httpx.Response(200, json={"status": "success", "datas": {"username": "u"}})
        if request.url.path.endswith("/boom"):
            return httpx.Response(
                500, json={"status": "error", "datas": {"msg": "mongo at db-host"}}
            )
        return httpx.Response(404, json={"status": "error", "datas": {"msg": "Audit not found"}})

    with TestClient(me_app(tmp_path, respond)) as c:
        boom = c.get("/__ai/api/audits/boom/findings", headers=BOB)
        assert boom.status_code == 502 and "db-host" not in boom.text and "mongo" not in boom.text
        missing = c.get("/__ai/api/audits/nope/findings", headers=BOB)
        assert missing.status_code == 404 and "Audit not found" in missing.json()["detail"]


# -- run(): refuse plain HTTP on the network unless explicitly allowed ---------------------------
@pytest.fixture
def started(monkeypatch):
    """Capture uvicorn.run instead of serving; restore the process umask run() changes."""
    import os

    calls: list[dict] = []
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **kw: calls.append(kw))
    monkeypatch.delenv("PWNDOC_AI_ALLOW_HTTP", raising=False)
    previous = os.umask(0o022)
    os.umask(previous)
    yield calls
    os.umask(previous)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "gateway.lan"])  # noqa: S104
def test_run_refuses_plain_http_on_a_network_address(started, tmp_path, host):
    with pytest.raises(server.InsecureBindError, match="--insecure-http"):
        server.run(host, 8080, str(tmp_path))
    assert started == []


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]"])
def test_run_allows_plain_http_on_loopback(started, tmp_path, host):
    server.run(host, 8080, str(tmp_path))
    assert started[0]["host"] == host and started[0]["proxy_headers"] is True


def test_run_allows_lan_http_only_with_explicit_consent(started, tmp_path, monkeypatch):
    server.run("0.0.0.0", 8080, str(tmp_path), insecure_http=True)  # noqa: S104
    monkeypatch.setenv("PWNDOC_AI_ALLOW_HTTP", "1")
    server.run("0.0.0.0", 8081, str(tmp_path))  # noqa: S104
    assert [c["port"] for c in started] == [8080, 8081]


def test_run_https_on_lan_needs_no_extra_flag_and_hands_cert_to_uvicorn(
    started, tmp_path, monkeypatch
):
    monkeypatch.setattr(server, "ensure_self_signed", lambda d: ("/c.crt", "/c.key"))
    server.run("0.0.0.0", 8443, str(tmp_path), https=True)  # noqa: S104
    assert started[0]["ssl_certfile"] == "/c.crt" and started[0]["ssl_keyfile"] == "/c.key"


def test_run_makes_new_files_owner_only(started, tmp_path):
    import os

    server.run("127.0.0.1", 8080, str(tmp_path))
    assert os.umask(0o077) == 0o077


@pytest.mark.parametrize(
    ("host", "expected"),
    [("localhost", True), ("127.0.0.1", True), ("127.9.9.9", True), ("::1", True),
     ("0.0.0.0", False), ("10.0.0.2", False), ("example.com", False)],  # noqa: S104
)  # fmt: skip
def test_is_loopback(host, expected):
    assert server.is_loopback(host) is expected
