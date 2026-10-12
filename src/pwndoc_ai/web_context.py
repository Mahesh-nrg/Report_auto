"""Optional internet research for a finding (DuckDuckGo search + readable page text).

Fetched URLs come from search results, i.e. untrusted input, so every request (and every
redirect hop) is refused if it resolves to a loopback/private/link-local address. This keeps the
gateway from being used to probe the LAN or cloud metadata endpoints. Resolution happens just
before the request; a hostile DNS server could still race it, so run the gateway on a network
segment you would be comfortable exposing to the public internet.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from collections.abc import Callable
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

MAX_PAGE_CHARS = 5600
MAX_REDIRECTS = 3
MAX_PAGE_BYTES = 1_500_000  # decoded bytes read per page (also bounds gzip bombs)
FETCH_DEADLINE = 15.0  # seconds for one page including redirects
REQUEST_DELAY = 0.4  # seconds between page fetches, be polite
USER_AGENT = "Mozilla/5.0 (compatible; pwndoc-ai/0.1)"
NOISE_TAGS = ("script", "style", "nav", "footer", "header", "aside", "form")
CONTENT_CLASS = re.compile(r"(content|post|article|entry-content)", re.I)

SearchFn = Callable[[str, int], list[str]]
FetchFn = Callable[[str], str]


class UnsafeUrlError(ValueError):
    pass


def assert_public_url(url: str) -> str:
    """Return the public IP the host resolves to, or raise :class:`UnsafeUrlError`.

    The caller connects to the returned address, so the checked IP is the one used (no
    second DNS lookup an attacker could answer differently).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeUrlError(f"unsupported URL: {url}")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"cannot resolve {parsed.hostname}") from exc
    chosen: str | None = None
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise UnsafeUrlError(f"{parsed.hostname} resolves to a non-public address")
        chosen = chosen or str(address)
    if chosen is None:
        raise UnsafeUrlError(f"cannot resolve {parsed.hostname}")
    return chosen


def search_urls(query: str, limit: int) -> list[str]:
    """DuckDuckGo text search returning result URLs."""
    from ddgs import DDGS

    try:
        with DDGS() as ddgs:
            return [r["href"] for r in ddgs.text(query, max_results=limit) if r.get("href")]
    except Exception as exc:  # noqa: BLE001 - search backends fail in many ways
        log.warning("web search failed: %s", exc)
        return []


def extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(list(NOISE_TAGS)):
        tag.decompose()
    root = (
        soup.find("article")
        or soup.find("main")
        or soup.find("div", class_=CONTENT_CLASS)
        or soup.body
    )
    if root is None:
        return ""
    lines = [
        el.get_text(strip=True)
        for el in root.find_all(["p", "h1", "h2", "h3", "li", "td", "th"])
        if len(el.get_text(strip=True)) > 10
    ]
    text = re.sub(r"\s{2,}", " ", " ".join(lines)).strip()
    return text[:MAX_PAGE_CHARS] + (" ... [truncated]" if len(text) > MAX_PAGE_CHARS else "")


def _pinned_request(http: httpx.Client, url: str, address: str) -> httpx.Request:
    """GET ``url`` but connect to the already-validated ``address`` (Host/SNI keep the name)."""
    parsed = urlparse(url)
    host = f"[{address}]" if ":" in address else address
    port = f":{parsed.port}" if parsed.port else ""
    pinned = urlunparse(parsed._replace(netloc=host + port))
    headers = {"Host": f"{parsed.hostname}{port}"}
    extensions = {"sni_hostname": parsed.hostname} if parsed.scheme == "https" else {}
    return http.build_request("GET", pinned, headers=headers, extensions=extensions)


def _read_capped(response: httpx.Response, deadline: float) -> str:
    body = bytearray()
    for chunk in response.iter_bytes():
        body += chunk
        if len(body) >= MAX_PAGE_BYTES:
            break
        if time.monotonic() > deadline:
            raise httpx.ReadTimeout("page fetch deadline exceeded")
    try:
        return bytes(body[:MAX_PAGE_BYTES]).decode(response.encoding or "utf-8", errors="replace")
    except LookupError:
        return bytes(body[:MAX_PAGE_BYTES]).decode("utf-8", errors="replace")


def fetch_page_text(url: str, transport: httpx.BaseTransport | None = None) -> str:
    """Fetch ``url`` (public hosts only, pinned IP, capped size/time) and return its text."""
    deadline = time.monotonic() + FETCH_DEADLINE
    current = url
    with httpx.Client(timeout=8, headers={"User-Agent": USER_AGENT}, transport=transport) as http:
        for _ in range(MAX_REDIRECTS + 1):
            address = assert_public_url(current)
            response = http.send(_pinned_request(http, current, address), stream=True)
            try:
                if response.is_redirect and response.headers.get("location"):
                    current = urljoin(current, response.headers["location"])
                    continue
                response.raise_for_status()
                if "html" not in response.headers.get("content-type", "html"):
                    return ""
                return extract_text(_read_capped(response, deadline))
            finally:
                response.close()
    raise UnsafeUrlError("too many redirects")


class WebContext:
    def __init__(
        self,
        results: int = 3,
        search: SearchFn = search_urls,
        fetch: FetchFn = fetch_page_text,
        delay: float = REQUEST_DELAY,
    ):
        self.results = results
        self._search = search
        self._fetch = fetch
        self._delay = delay

    def for_finding(self, title: str) -> str:
        if self.results <= 0 or not title.strip():
            return ""
        query = f"{title} vulnerability CWE ID OWASP Top 10 remediation"
        blocks: list[str] = []
        for index, url in enumerate(self._search(query, self.results), 1):
            try:
                text = self._fetch(url)
            except (httpx.HTTPError, UnsafeUrlError, ValueError) as exc:
                log.info("skipping %s: %s", url, exc)
                continue
            if text:
                blocks.append(f"[Web result {index}]\n{text}\nSource: {url}")
            time.sleep(self._delay)
        return "\n\n".join(blocks)
