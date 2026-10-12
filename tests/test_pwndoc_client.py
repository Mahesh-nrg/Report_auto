import httpx
import pytest

from pwndoc_ai.pwndoc_client import PwnDocClient, PwnDocError


def make_client(handler, **kwargs):
    return PwnDocClient(
        "https://pwndoc.test:8443/", transport=httpx.MockTransport(handler), **kwargs
    )


def test_login_posts_credentials_and_keeps_cookie():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/users/token":
            return httpx.Response(
                200,
                json={"status": "success", "datas": {"token": "x"}},
                headers={"set-cookie": "token=JWT%20abc; Path=/"},
            )
        return httpx.Response(200, json={"status": "success", "datas": {"username": "bob"}})

    with make_client(handler) as client:
        client.login("bob", "pw", "123456")
        assert client.me() == {"username": "bob"}

    assert seen[0].method == "POST"
    body = httpx.Response(200, content=seen[0].content).json()
    assert body == {"username": "bob", "password": "pw", "totpToken": "123456"}
    assert "token=JWT%20abc" in seen[1].headers.get("cookie", "")


def test_base_url_trailing_slash_is_stripped():
    with make_client(lambda r: httpx.Response(200, json={"datas": []})) as client:
        assert client.base_url == "https://pwndoc.test:8443"


def test_forwarded_cookies_are_sent_verbatim():
    captured = {}

    def handler(request):
        captured["cookie"] = request.headers.get("cookie")
        return httpx.Response(200, json={"datas": []})

    with make_client(handler, cookies={"token": "JWT%20abc"}) as client:
        client.list_audits()
    assert captured["cookie"] == "token=JWT%20abc"


def test_error_message_from_datas_msg():
    def handler(request):
        return httpx.Response(403, json={"status": "error", "datas": {"msg": "Forbidden"}})

    with make_client(handler) as client, pytest.raises(PwnDocError) as info:
        client.list_audits()
    assert info.value.status == 403
    assert "Forbidden" in str(info.value)


def test_error_message_for_non_json_body():
    with make_client(lambda r: httpx.Response(502, text="bad gateway")) as client:
        with pytest.raises(PwnDocError) as info:
            client.get_audit("a1")
    assert info.value.status == 502
    assert "502" in str(info.value)


def test_update_finding_strips_id_and_uses_put():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = request.content
        return httpx.Response(200, json={"status": "success", "datas": "ok"})

    with make_client(handler) as client:
        client.update_finding("aud1", "f1", {"_id": "f1", "title": "T", "poc": "<p>x</p>"})

    assert captured["method"] == "PUT"
    assert captured["path"] == "/api/audits/aud1/findings/f1"
    payload = httpx.Response(200, content=captured["body"]).json()
    assert payload == {"title": "T", "poc": "<p>x</p>"}


def test_list_findings_reads_from_audit():
    def handler(request):
        return httpx.Response(200, json={"datas": {"findings": [{"_id": "f1"}]}})

    with make_client(handler) as client:
        assert client.list_findings("a") == [{"_id": "f1"}]


def test_export_docx_returns_bytes():
    def handler(request):
        return httpx.Response(
            200, content=b"PK\x03\x04", headers={"content-type": "application/octet-stream"}
        )

    with make_client(handler) as client:
        assert client.export_docx("a") == b"PK\x03\x04"


def test_unreachable_host_raises_pwndoc_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with make_client(handler) as client, pytest.raises(PwnDocError) as info:
        client.me()
    assert "cannot reach PwnDoc" in str(info.value)
    assert info.value.status is None


def test_invalid_json_body_becomes_pwndoc_error():
    def handler(request):
        return httpx.Response(200, content=b"<html>", headers={"content-type": "application/json"})

    with make_client(handler) as client, pytest.raises(PwnDocError, match="invalid JSON"):
        client.list_audits()


def test_me_must_be_an_object():
    def handler(request):
        return httpx.Response(200, json={"status": "success", "datas": "nope"})

    with make_client(handler) as client, pytest.raises(PwnDocError, match="users/me"):
        client.me()


def test_export_uses_long_timeout():
    seen = {}

    def handler(request):
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(
            200, content=b"PK", headers={"content-type": "application/octet-stream"}
        )

    with make_client(handler) as client:
        assert client.export_docx("a1") == b"PK"
    assert seen["timeout"]["read"] == 120.0
