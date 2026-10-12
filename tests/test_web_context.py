import socket

import pytest

from pwndoc_ai import web_context
from pwndoc_ai.web_context import (
    MAX_PAGE_CHARS,
    UnsafeUrlError,
    WebContext,
    assert_public_url,
    extract_text,
)


def fake_resolver(mapping):
    def getaddrinfo(host, port, proto=0):
        if host not in mapping:
            raise socket.gaierror("nope")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], port))]

    return getaddrinfo


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://10.1.2.3/",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.10:8443/",
        "file:///etc/passwd",
        "ftp://example.com/",
        "http:///nohost",
    ],
)
def test_unsafe_urls_rejected(url, monkeypatch):
    monkeypatch.setattr(
        web_context.socket,
        "getaddrinfo",
        lambda host, port, proto=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))],
    )
    with pytest.raises(UnsafeUrlError):
        assert_public_url(url)


def test_hostname_resolving_to_private_is_rejected(monkeypatch):
    monkeypatch.setattr(web_context.socket, "getaddrinfo", fake_resolver({"evil.test": "10.0.0.5"}))
    with pytest.raises(UnsafeUrlError):
        assert_public_url("https://evil.test/")


def test_unresolvable_host_rejected(monkeypatch):
    monkeypatch.setattr(web_context.socket, "getaddrinfo", fake_resolver({}))
    with pytest.raises(UnsafeUrlError):
        assert_public_url("https://missing.test/")


def test_public_address_allowed(monkeypatch):
    monkeypatch.setattr(
        web_context.socket, "getaddrinfo", fake_resolver({"ok.test": "93.184.216.34"})
    )
    assert_public_url("https://ok.test/page")


def test_extract_text_removes_noise_and_prefers_article():
    html = """
    <html><body>
      <nav><p>Navigation menu should vanish entirely</p></nav>
      <script>var secret = 'do not include this script text';</script>
      <article>
        <h1>Cross-site scripting overview</h1>
        <p>Reflected XSS occurs when input is echoed unescaped.</p>
        <p>short</p>
      </article>
      <footer><p>Footer links should vanish entirely too</p></footer>
    </body></html>
    """
    text = extract_text(html)
    assert "Cross-site scripting overview" in text
    assert "Reflected XSS" in text
    assert "Navigation" not in text and "Footer" not in text and "secret" not in text
    assert "short" not in text  # shorter than the minimum line length


def test_extract_text_truncates():
    html = "<article>" + "".join(f"<p>{'word ' * 20}{i}</p>" for i in range(200)) + "</article>"
    text = extract_text(html)
    assert text.endswith("[truncated]")
    assert len(text) <= MAX_PAGE_CHARS + len(" ... [truncated]")


def test_extract_text_empty_document():
    assert extract_text("") == ""


def test_for_finding_builds_blocks_and_skips_unsafe():
    def search(query, limit):
        assert "SQL injection" in query
        return ["https://a.test/1", "http://10.0.0.1/internal", "https://b.test/2"]

    def fetch(url):
        if "10.0.0.1" in url:
            raise UnsafeUrlError("private")
        return f"text from {url}"

    ctx = WebContext(results=3, search=search, fetch=fetch, delay=0)
    out = ctx.for_finding("SQL injection in login")
    assert "[Web result 1]\ntext from https://a.test/1\nSource: https://a.test/1" in out
    assert "[Web result 3]" in out
    assert "10.0.0.1" not in out


def test_for_finding_skips_empty_pages():
    ctx = WebContext(results=2, search=lambda q, n: ["https://a.test"], fetch=lambda u: "", delay=0)
    assert ctx.for_finding("x") == ""


def test_zero_results_or_blank_title_returns_empty():
    def boom(*args):
        raise AssertionError("must not be called")

    assert WebContext(results=0, search=boom, fetch=boom).for_finding("title") == ""
    assert WebContext(results=3, search=boom, fetch=boom).for_finding("   ") == ""


# -- pinned, capped, redirect-safe fetching -------------------------------------------------------
import httpx  # noqa: E402

from pwndoc_ai.web_context import fetch_page_text  # noqa: E402

PAGE = "<article><p>Cross-site scripting is a client-side injection weakness.</p></article>"


def resolver(monkeypatch, mapping):
    monkeypatch.setattr(web_context.socket, "getaddrinfo", fake_resolver(mapping))


def test_assert_public_url_returns_the_validated_address(monkeypatch):
    resolver(monkeypatch, {"ok.test": "93.184.216.34"})
    assert assert_public_url("https://ok.test/") == "93.184.216.34"


def test_fetch_connects_to_validated_ip_keeping_host_and_sni(monkeypatch):
    resolver(monkeypatch, {"ok.test": "93.184.216.34"})
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})

    text = fetch_page_text("https://ok.test:8443/a?b=1", transport=httpx.MockTransport(handler))
    assert "Cross-site scripting" in text
    (request,) = seen
    assert request.url.host == "93.184.216.34" and request.url.port == 8443
    assert request.url.query == b"b=1"
    assert request.headers["host"] == "ok.test:8443"
    assert request.extensions["sni_hostname"] == "ok.test"


def test_dns_rebinding_cannot_swap_the_address_between_check_and_connect(monkeypatch):
    answers = iter(["93.184.216.34", "127.0.0.1"])  # a hostile resolver's second answer
    monkeypatch.setattr(
        web_context.socket,
        "getaddrinfo",
        lambda host, port, proto=0: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port))
        ],
    )
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})

    fetch_page_text("https://rebind.test/", transport=httpx.MockTransport(handler))
    assert hosts == ["93.184.216.34"]  # one lookup; the connection used the checked address


def test_redirect_to_private_address_is_blocked(monkeypatch):
    resolver(monkeypatch, {"ok.test": "93.184.216.34", "inner.test": "10.0.0.8"})

    def handler(request):
        return httpx.Response(302, headers={"location": "http://inner.test/admin"})

    with pytest.raises(UnsafeUrlError):
        fetch_page_text("https://ok.test/", transport=httpx.MockTransport(handler))


def test_redirects_are_followed_and_revalidated_up_to_a_limit(monkeypatch):
    resolver(monkeypatch, {"a.test": "93.184.216.34", "b.test": "93.184.216.35"})

    def handler(request):
        if request.headers["host"] == "a.test":
            return httpx.Response(301, headers={"location": "https://b.test/final"})
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})

    assert "Cross-site" in fetch_page_text(
        "https://a.test/", transport=httpx.MockTransport(handler)
    )

    def loop(request):
        return httpx.Response(302, headers={"location": "https://a.test/again"})

    with pytest.raises(UnsafeUrlError, match="redirects"):
        fetch_page_text("https://a.test/", transport=httpx.MockTransport(loop))


def test_body_is_capped_so_huge_pages_cannot_exhaust_memory(monkeypatch):
    resolver(monkeypatch, {"big.test": "93.184.216.34"})
    monkeypatch.setattr(web_context, "MAX_PAGE_BYTES", 2000)
    head = "<article><p>Visible paragraph near the start of the page.</p>"
    tail_marker = "<p>TAIL MARKER beyond the byte cap must never be read</p>"
    body = head + "<p>" + "x" * 50_000 + "</p>" + tail_marker

    def handler(request):
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    text = fetch_page_text("https://big.test/", transport=httpx.MockTransport(handler))
    assert "Visible paragraph" in text and "TAIL MARKER" not in text


def test_non_html_and_http_errors(monkeypatch):
    resolver(monkeypatch, {"f.test": "93.184.216.34"})
    pdf = httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
    )
    assert fetch_page_text("https://f.test/x.pdf", transport=pdf) == ""
    err = httpx.MockTransport(lambda r: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_page_text("https://f.test/", transport=err)


def test_total_deadline_aborts_slow_drip(monkeypatch):
    resolver(monkeypatch, {"slow.test": "93.184.216.34"})
    clock = iter([0.0, 0.0, 100.0, 200.0, 300.0])
    monkeypatch.setattr(web_context.time, "monotonic", lambda: next(clock))

    def handler(request):
        return httpx.Response(
            200,
            content=iter([b"<p>a</p>", b"<p>b</p>", b"<p>c</p>"]),
            headers={"content-type": "text/html"},
        )

    with pytest.raises(httpx.ReadTimeout):
        fetch_page_text("https://slow.test/", transport=httpx.MockTransport(handler))
