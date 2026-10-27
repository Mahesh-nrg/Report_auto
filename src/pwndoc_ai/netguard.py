"""SSRF guard for URLs an administrator can type in (webhook, LanguageTool, LLM endpoint).

Cloud metadata services answer on link-local addresses and well-known names; nothing legitimate
lives there, so a URL pointing at one is an SSRF attempt or a mistake. Loopback and ordinary
private ranges stay allowed: a LanguageTool on ``localhost:8010`` or a LAN LLM proxy is normal.

Every spelling an HTTP client or the OS resolver would accept is understood: decimal, hex, octal
and short IPv4 forms, IPv6 wrappers of IPv4 (mapped, NAT64, SIIT, compatible, 6to4, Teredo),
full-width digits, percent-encoding and zone ids.
Host names are resolved and refused when ANY answer is blocked. Callers that go on to connect
should use :func:`inspect_http_url` and connect to the addresses it returns, so the address that
was checked is the address that is used (no second lookup an attacker's DNS could answer
differently).
"""

from __future__ import annotations

import ipaddress
import re
import socket
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

IPv4 = ipaddress.IPv4Address
IPv6 = ipaddress.IPv6Address

METADATA_HOSTS = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
        "metadata.tencentyun.com",
    }
)
METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(a)
    for a in ("169.254.169.254", "fd00:ec2::254", "100.100.100.200", "192.0.0.192")
)
# IPv6 prefixes whose last 32 bits are an IPv4 address: NAT64, SIIT and the deprecated
# IPv4-compatible form. (6to4 and Teredo keep it elsewhere; ipaddress decodes those.)
_LOW_WRAPPERS = tuple(ipaddress.ip_network(n) for n in ("64:ff9b::/96", "::ffff:0:0:0/96", "::/96"))
MAX_PINNED_ADDRESSES = 4
BLOCKED_MESSAGE = "{name} must not point to a link-local or cloud-metadata address"
_DOTS = str.maketrans({"。": ".", "．": ".", "｡": "."})
_BAD_URL_CHARS = re.compile(r"[\x00-\x20\x7f-\x9f\\]")
_DECIMAL = re.compile(r"[1-9][0-9]*|0")
_OCTAL = re.compile(r"0[0-7]+")
_HEX = re.compile(r"0[xX][0-9a-fA-F]*")
_MAX_IPV4_PARTS = 4


@dataclass(frozen=True)
class SafeTarget:
    """A URL that passed the guard, with the addresses that were checked."""

    url: str
    scheme: str
    host: str
    port: int | None
    addresses: tuple[str, ...]  # what the host resolved to when it was checked (may be empty)
    is_literal: bool  # the host is an IP literal: there is no lookup an attacker could change


def _canonical_host(host: object) -> str:
    """The host as a resolver would see it: no brackets, zone, case, trailing dot or odd forms."""
    name = unicodedata.normalize("NFKC", str(host or "")).translate(_DOTS)
    name = unquote(name.strip().strip("[]")).partition("%")[0]
    return name.rstrip(".").lower()


def _number(part: str) -> int | None:
    """One part of a dotted IPv4 address in decimal, octal or hex, as inet_aton reads it."""
    if _HEX.fullmatch(part):
        return int(part[2:] or "0", 16)
    if _OCTAL.fullmatch(part):
        return int(part, 8)
    if _DECIMAL.fullmatch(part):
        return int(part)
    return None


def _inet_aton(name: str) -> IPv4 | None:
    """IPv4 in any legacy spelling (``2852039166``, ``0xa9fea9fe``, ``0251.0376.43518``)."""
    parts = name.split(".")
    if not 1 <= len(parts) <= _MAX_IPV4_PARTS:
        return None
    numbers = [_number(p) for p in parts]
    if any(n is None for n in numbers):
        return None
    *head, last = numbers
    if any(n > 255 for n in head) or last >= 256 ** (_MAX_IPV4_PARTS - len(head)):
        return None
    value = last
    for position, number in enumerate(head):
        value += number << (8 * (_MAX_IPV4_PARTS - 1 - position))
    return IPv4(value)


def _parse_address(name: str) -> IPv4 | IPv6 | None:
    try:
        return ipaddress.ip_address(name)
    except ValueError:
        return _inet_aton(name)


def _embedded_ipv4(address: IPv4 | IPv6) -> list[IPv4]:
    """Every IPv4 address an IPv6 one wraps: mapped, NAT64, SIIT, compatible, 6to4 and Teredo."""
    if isinstance(address, IPv4):
        return [address]
    found: list[IPv4] = []
    if address.ipv4_mapped is not None:
        found.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        found.append(address.sixtofour)
    if address.teredo is not None:
        found.extend(address.teredo)
    if any(address in prefix for prefix in _LOW_WRAPPERS):
        found.append(IPv4(int(address) & 0xFFFFFFFF))
    return found


def _address_blocked(address: IPv4 | IPv6) -> bool:
    candidates = [address, *_embedded_ipv4(address)]
    return any(
        a in METADATA_ADDRESSES or a.is_link_local or a.is_unspecified or a.is_multicast
        for a in candidates
    )


def _resolve(name: str) -> list[str] | None:
    """Every address ``name`` resolves to; ``[]`` when it does not; ``None`` when malformed."""
    try:
        infos = socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
    except (UnicodeError, ValueError):
        return None
    except OSError:  # includes gaierror: it will not connect either
        return []
    return list(dict.fromkeys(str(info[4][0]).partition("%")[0] for info in infos))


def _addresses_blocked(addresses: list[str]) -> bool:
    for text in addresses:
        parsed = _parse_address(text)
        if parsed is None or _address_blocked(parsed):
            return True
    return False


def host_is_blocked(host: str | None, *, resolve: bool = True) -> bool:
    """True for link-local / unspecified / multicast / cloud-metadata hosts, in any spelling.

    With ``resolve`` a host name is looked up and blocked when ANY of its addresses is. A name
    that does not resolve is not blocked (it cannot connect). Loopback and private ranges are
    allowed.
    """
    name = _canonical_host(host)
    if not name or re.search(r"[\x00-\x20\x7f-\x9f]", name):
        return True
    if name in METADATA_HOSTS:
        return True
    parsed = _parse_address(name)
    if parsed is not None:
        return _address_blocked(parsed)
    if not resolve:
        return False
    answers = _resolve(name)
    return answers is None or _addresses_blocked(answers)


def _split(url: str, name: str) -> tuple[str, str, str, int | None]:
    """Scheme, host (as urlsplit and httpx both read it) and port of a plain http(s) URL."""
    if _BAD_URL_CHARS.search(url):
        raise ValueError(f"{name} must be a http(s) URL")
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname or ""
        client_host = httpx.URL(url).host
    except (ValueError, httpx.InvalidURL) as exc:
        raise ValueError(f"{name} must be a http(s) URL") from exc
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError(f"{name} must be a http(s) URL")
    if _canonical_host(client_host) != _canonical_host(host):
        raise ValueError(f"{name} must be a http(s) URL")  # parsers disagree: refuse to guess
    return parts.scheme, host, url, port


def inspect_http_url(url: str, name: str, *, resolve: bool = True) -> SafeTarget:
    """Validate an http(s) URL and return the host's checked addresses (``ValueError`` if bad).

    Raises with a short message that never contains the URL (it may embed a secret token).
    """
    text = str(url or "").strip()
    scheme, host, text, port = _split(text, name)
    canonical = _canonical_host(host)
    literal = _parse_address(canonical)
    if host_is_blocked(host, resolve=False):
        raise ValueError(BLOCKED_MESSAGE.format(name=name))
    if literal is not None:
        return SafeTarget(text, scheme, host, port, (str(literal),), True)
    answers = _resolve(canonical) if resolve else []
    if answers is None or _addresses_blocked(answers):
        raise ValueError(BLOCKED_MESSAGE.format(name=name))
    return SafeTarget(text, scheme, host, port, tuple(answers), False)


def assert_safe_http_url(url: str, name: str) -> str:
    """The stripped URL when it is a usable http(s) URL outside link-local/metadata space.

    Raises ``ValueError`` (short message, never the URL) otherwise. Host names are resolved.
    """
    return inspect_http_url(url, name).url


def pinned_requests(
    client: httpx.Client, target: SafeTarget, method: str, *, pinned: bool = True, **kwargs: Any
) -> Iterator[httpx.Request]:
    """The requests to try for a checked target, one per checked address (at most a few).

    Pinned, each request connects to an address the guard checked and carries the real host name
    (``Host`` header, TLS name and certificate check), so no second lookup can answer differently.
    Not pinned (a caller-supplied transport, in tests) there is a single request to the URL.
    """
    if not pinned:
        yield client.build_request(method, target.url, **kwargs)
        return
    url = httpx.URL(target.url)
    name = f"[{target.host}]" if ":" in target.host else target.host  # IPv6 literal
    host_header = name if target.port is None else f"{name}:{target.port}"
    # a literal host is rewritten to its canonical address too: no parser or resolver can read
    # a legacy spelling (``0x7f.1``) differently from the guard
    sni = {} if target.is_literal else {"sni_hostname": target.host}  # TLS name and cert check
    headers = {**kwargs.pop("headers", {}), "Host": host_header}
    for address in target.addresses[:MAX_PINNED_ADDRESSES]:
        yield client.build_request(
            method, url.copy_with(host=address), headers=headers, extensions=sni, **kwargs
        )
