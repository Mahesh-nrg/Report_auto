"""Optional internet research for a finding (DuckDuckGo search + readable page text).

Fetched URLs come from search results, i.e. untrusted input, so every request (and every
redirect hop) is refused if it resolves to a loopback/private/link-local address. This keeps the
gateway from being used to probe the LAN or cloud metadata endpoints. Resolution happens just
before the request; a hostile DNS server could still race it, so run the gateway on a network
segment you would be comfortable exposing to the public internet.
"""

from __future__ import annotations

import functools
import ipaddress
import logging
import re
import socket
import time
from collections.abc import Callable, Sequence
from typing import NamedTuple
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from .netguard import _embedded_ipv4

log = logging.getLogger(__name__)

MAX_PAGE_CHARS = 5600  # what the truncation fallback shows of one page
MAX_RANK_PAGE_CHARS = 16000  # what is kept of one page so the best passages can be ranked
TRUNCATION_MARKER = " ... [truncated]"
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


class WebFindings(NamedTuple):
    """Research text plus the hosts (``netloc`` without credentials) that supplied it."""

    text: str
    hosts: tuple[str, ...]


class WebPage(NamedTuple):
    """One fetched search result: ``index`` is its 1-based position in the search results.

    ``url`` and ``host`` are where the text really came from (after any redirect); ``via`` is
    the host of the search result when that was a different one, else empty.
    """

    url: str
    host: str  # ``host_of(url)``: no credentials, may be empty
    text: str
    index: int
    via: str = ""


class PageText(str):
    """Page text that remembers the address it was finally fetched from (after redirects)."""

    final_url: str

    def __new__(cls, text: str, final_url: str) -> PageText:
        obj = super().__new__(cls, text)
        obj.final_url = final_url
        return obj


def host_of(url: str) -> str:
    """``netloc`` of ``url`` with any ``user:password@`` part removed (``""`` if unparsable)."""
    try:
        return urlparse(url).netloc.rsplit("@", 1)[-1]
    except ValueError:  # e.g. an unterminated IPv6 literal
        return ""


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
        # NAT64, SIIT and the like wrap an IPv4 address: it must be a public one as well
        if not all(a.is_global for a in (address, *_embedded_ipv4(address))):
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
        log.warning("web search failed (%s)", type(exc).__name__)  # the message may hold the query
        return []


def extract_text(html: str, max_chars: int = MAX_PAGE_CHARS) -> str:
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
    return text[:max_chars] + (TRUNCATION_MARKER if len(text) > max_chars else "")


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


def fetch_page_text(
    url: str, transport: httpx.BaseTransport | None = None, max_chars: int = MAX_PAGE_CHARS
) -> str:
    """Fetch ``url`` (public hosts only, pinned IP, capped size/time) and return its text.

    The result is a :class:`PageText`: its ``final_url`` is the address after any redirect.
    """
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
                    return PageText("", current)
                return PageText(extract_text(_read_capped(response, deadline), max_chars), current)
            finally:
                response.close()
    raise UnsafeUrlError("too many redirects")


def _page(url: str, text: str, index: int) -> WebPage:
    """The page as fetched: labelled with the host the text came from, not the one searched."""
    final = str(getattr(text, "final_url", "") or url)
    host, first = host_of(final), host_of(url)
    return WebPage(final, host, str(text), index, via=first if first != host else "")


def _fallback_text(text: str) -> str:
    """A page cut to the length the pre-ranking pipeline used (marker kept as it was)."""
    text = text.removesuffix(TRUNCATION_MARKER)
    if len(text) <= MAX_PAGE_CHARS:
        return text
    return text[:MAX_PAGE_CHARS] + TRUNCATION_MARKER


class WebContext:
    """Searches the web for a finding and fetches the result pages (nothing is ranked here)."""

    def __init__(
        self,
        results: int = 3,
        search: SearchFn = search_urls,
        fetch: FetchFn | None = None,
        delay: float = REQUEST_DELAY,
    ):
        self.results = results
        self._search = search
        self._fetch = fetch or functools.partial(fetch_page_text, max_chars=MAX_RANK_PAGE_CHARS)
        self._delay = delay

    def for_finding(self, title: str) -> str:
        return self.search_finding(title).text

    def search_finding(self, title: str) -> WebFindings:
        """The truncated first-characters text of every page (used when nothing can be ranked)."""
        return self.format_pages(self.fetch_pages(title))

    def fetch_pages(self, title: str) -> list[WebPage]:
        """Search for ``title`` and fetch each result (SSRF/size/time guards live in the fetcher).

        Only the title goes to the search engine. A result that cannot be fetched or has no text
        is skipped; one bad page never fails the lookup.
        """
        if self.results <= 0 or not title.strip():
            return []
        query = f"{title} vulnerability CWE ID OWASP Top 10 remediation"
        pages: list[WebPage] = []
        for index, url in enumerate(self._search(query, self.results), 1):
            try:
                text = self._fetch(url)
            except (httpx.HTTPError, UnsafeUrlError, ValueError) as exc:
                log.info("skipping a result from %s (%s)", host_of(url) or "?", type(exc).__name__)
                continue
            if text:
                pages.append(_page(url, text, index))
            time.sleep(self._delay)
        return pages

    def format_pages(self, pages: Sequence[WebPage]) -> WebFindings:
        blocks = [
            f"[Web result {p.index}]\n{_fallback_text(p.text)}\nSource: {p.url}" for p in pages
        ]
        hosts = tuple(dict.fromkeys(p.host for p in pages if p.host))
        return WebFindings("\n\n".join(blocks), hosts)
