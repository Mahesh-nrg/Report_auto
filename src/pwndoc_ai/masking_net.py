"""Domain, host, IPv4/IPv6 and e-mail mapping for the realistic-dummy masking.

Pure helpers plus a ``NetMapper`` that holds the registry of fake registrable domains, the keyed
``/24`` IPv4 map and the IPv6 map. Rewrites keep the structure the model needs: sub-domain labels,
URL path/query/fragment/port, IP host octet and address class, generic e-mail role local parts.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache

from .masking_model import (
    FAKE_PUBLIC_V4_POOLS,
    FAKE_V6_POOL,
    MAX_DUMMY_ATTEMPTS,
    ROLE_LOCAL_PARTS,
    MaskingError,
    canonical_ipv4,
    in_fake_pool,
    is_unmasked_ipv4,
    pad_like,
    private_kind,
)
from .masking_names import FIRST_NAMES, LAST_NAMES, ROOTS

# Common multi-label public suffixes, so ``shop.example.co.uk`` keeps ``example.co.uk`` whole.
MULTI_SUFFIXES = frozenset(
    {
        "co.uk",
        "org.uk",
        "gov.uk",
        "ac.uk",
        "me.uk",
        "ltd.uk",
        "plc.uk",
        "net.uk",
        "com.au",
        "net.au",
        "org.au",
        "gov.au",
        "edu.au",
        "id.au",
        "co.jp",
        "or.jp",
        "ne.jp",
        "go.jp",
        "ac.jp",
        "co.nz",
        "org.nz",
        "govt.nz",
        "ac.nz",
        "co.za",
        "org.za",
        "gov.za",
        "com.br",
        "com.cn",
        "com.mx",
        "com.sg",
        "com.hk",
        "com.tr",
        "com.tw",
        "co.in",
        "co.kr",
        "co.il",
        "com.ua",
        "co.id",
        "or.id",
    }
)

FAKE_ZONE = "example"  # reserved per RFC 2606; a fake registrable domain is <slug>.example


@dataclass(frozen=True)
class HostParts:
    """A host split into kept sub-domain labels and the registrable domain (eTLD+1)."""

    subdomains: tuple[str, ...]
    registrable: str


@lru_cache(maxsize=4096)
def decode_host(host: str) -> str:
    """Lower-case NFKC-folded ASCII host: ``xn--`` labels decoded, full-width folded."""
    host = host.strip().rstrip(".")
    try:
        host = host.encode("ascii").decode("idna") if "xn--" in host.lower() else host
    except (UnicodeError, ValueError):
        pass
    folded = unicodedata.normalize("NFKC", host).lower()
    try:
        return folded.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return folded


@lru_cache(maxsize=4096)
def registrable_domain(host: str) -> HostParts | None:
    """Split a host into sub-domains and its registrable domain, or None if it is not a domain."""
    labels = [x for x in decode_host(host).split(".") if x]
    if len(labels) < 2 or not all(re.fullmatch(r"[a-z0-9-]{1,63}", x) for x in labels):
        return None
    for span in (3, 2):
        if len(labels) >= span and ".".join(labels[-span:]) in MULTI_SUFFIXES:
            keep = span + 1
            return (
                HostParts(tuple(labels[:-keep]), ".".join(labels[-keep:]))
                if len(labels) >= keep
                else HostParts((), ".".join(labels))
            )
    return HostParts(tuple(labels[:-2]), ".".join(labels[-2:]))


def is_ipish(host: str) -> bool:
    """True when ``host`` is an IP literal (so it is not treated as a domain)."""
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _keyed(salt: bytes, key: str, modulo: int) -> int:
    digest = hmac.new(salt, key.encode(), hashlib.sha256).digest()
    return int.from_bytes(digest[:8], "big") % modulo


def _fictional_slug(salt: bytes, key: str) -> str:
    """A fictional lower-case domain label derived from the salt, never the real label."""
    return ROOTS[_keyed(salt, "dom\x00" + key, len(ROOTS))].lower()


@dataclass
class NetMapper:
    """Per-instance registry of fake domains and IP prefixes (guarded by the Masker's lock).

    ``is_free`` vetoes a candidate fake domain (reserved corpus text, issued stand-ins) and
    ``prefix_blocked`` vetoes a fake ``/24`` that occurs in the reserved corpus.
    """

    salt: bytes
    is_free: Callable[[str, bool], bool] = lambda _candidate, _hinted: True
    prefix_blocked: Callable[[tuple[int, int, int]], bool] = lambda _prefix: False
    _domains: dict[str, str] = field(default_factory=dict)
    _domain_used: set[str] = field(default_factory=set)
    _v4_prefix: dict[tuple[bool, int], tuple[int, int, int]] = field(default_factory=dict)
    _v4_used: set[tuple[int, int, int]] = field(default_factory=set)
    _v4_real: dict[tuple[int, ...], int] = field(default_factory=dict)
    _v6: dict[int, int] = field(default_factory=dict)
    _v6_used: set[int] = field(default_factory=set)

    # -- domains ---------------------------------------------------------------------------

    def fake_registrable(self, registrable: str, slug_hint: str | None = None) -> str:
        """Fake ``<slug>.example`` for a real registrable domain; stable, distinct per real."""
        key = registrable.lower()
        existing = self._domains.get(key)
        if existing is not None:
            return existing
        hint = re.sub(r"[^a-z0-9]", "", (slug_hint or "").lower())
        for attempt in range(MAX_DUMMY_ATTEMPTS):
            hinted = attempt == 0 and bool(hint)
            base = hint if hinted else _fictional_slug(self.salt, f"{key}#{attempt}")
            if attempt >= MAX_DUMMY_ATTEMPTS // 2:
                base += str(_keyed(self.salt, f"{key}{attempt}", 900) + 100)
            candidate = f"{base}.{FAKE_ZONE}"
            if candidate not in self._domain_used and self.is_free(candidate, hinted):
                self._domains[key] = candidate
                self._domain_used.add(candidate)
                return candidate
        raise MaskingError("no free stand-in domain could be generated")

    def has_domain(self, registrable: str) -> bool:
        """True when a fake domain was already issued for ``registrable``."""
        return registrable.lower() in self._domains

    def fake_host(self, host: str, slug_hint: str | None = None) -> str | None:
        """Fake host keeping the sub-domain labels, or None when ``host`` is not a domain."""
        parts = registrable_domain(host)
        if parts is None:
            return None
        fake = self.fake_registrable(parts.registrable, slug_hint)
        return ".".join((*parts.subdomains, fake)) if parts.subdomains else fake

    def issued_domains(self) -> dict[str, str]:
        """Issued fake registrable domain -> real registrable domain (a copy)."""
        return {fake: real for real, fake in self._domains.items()}

    def forget_domain(self, fake: str) -> None:
        """Drop an unsent fake domain so the real one gets a new stand-in next time."""
        for real, issued in tuple(self._domains.items()):
            if issued == fake:
                del self._domains[real]

    # -- IPv4 ------------------------------------------------------------------------------

    def fake_ipv4(self, address: str) -> str | None:
        """Fake IPv4 keeping the host octet and class, or None when it must stay as written."""
        canonical = canonical_ipv4(address)
        if is_unmasked_ipv4(canonical):
            return None
        try:
            ip = ipaddress.IPv4Address(canonical)
        except ValueError:
            return None
        key = (private_kind(canonical) is not None, int(ip) >> 8)
        prefix = self._v4_prefix.get(key)
        if prefix is None:
            prefix = self._pick_v4_prefix(private_kind(canonical), int(ip) >> 8)
            self._v4_prefix[key] = prefix
            self._v4_used.add(prefix)
            self._v4_real[prefix] = key[1]
        fake = ".".join((*(str(o) for o in prefix), str(int(ip) & 0xFF)))
        return pad_like(fake, address)

    def real_ipv4(self, address: str) -> str | None:
        """The real address behind any host of an issued fake ``/24`` (host octet kept)."""
        octets = canonical_ipv4(address).split(".")
        if not self._v4_real or len(octets) != 4 or not all(o.isdigit() for o in octets):
            return None
        real = self._v4_real.get(tuple(int(o) for o in octets[:3]))
        if real is None:
            return None
        restored = f"{real >> 16}.{(real >> 8) & 0xFF}.{real & 0xFF}.{octets[3]}"
        return pad_like(restored, address)

    def issued_v4_prefix(self, address: str) -> bool:
        """True when ``address`` lies in a fake ``/24`` this mapper has issued."""
        return self.real_ipv4(address) is not None

    def _candidate_prefix(self, kind: ipaddress.IPv4Network | None, key: str) -> int:
        pool = kind if kind is not None else FAKE_PUBLIC_V4_POOLS[_keyed(self.salt, key, 3)]
        span = max(1, pool.num_addresses // 256)
        return (int(pool.network_address) >> 8) + _keyed(self.salt, key + "/p", span)

    def _pick_v4_prefix(self, kind: ipaddress.IPv4Network | None, real: int) -> tuple[int, ...]:
        prefix: tuple[int, ...] = ()
        for attempt in range(MAX_DUMMY_ATTEMPTS * 4):
            net24 = self._candidate_prefix(kind, f"{real}#{attempt}")
            prefix = ((net24 >> 16) & 0xFF, (net24 >> 8) & 0xFF, net24 & 0xFF)
            if net24 != real and prefix not in self._v4_used and not self.prefix_blocked(prefix):
                return prefix
        raise MaskingError("no free stand-in address range could be generated")

    def forget_ipv4(self, fake: str) -> None:
        """Drop the ``/24`` behind an unsent fake address."""
        prefix = tuple(int(o) for o in fake.split(".")[:3])
        for real, issued in tuple(self._v4_prefix.items()):
            if issued == prefix:
                del self._v4_prefix[real]
                self._v4_real.pop(prefix, None)

    # -- IPv6 ------------------------------------------------------------------------------

    def fake_ipv6(self, address: str) -> str | None:
        """Fake ``2001:db8::/32`` address: keyed 32-bit network part, low 64 bits kept."""
        try:
            ip = ipaddress.IPv6Address(address)
        except ValueError:
            return None
        if ip.is_loopback or ip.is_unspecified or ip.is_link_local or ip.is_multicast:
            return None
        net64 = int(ip) >> 64
        fake_net = self._v6.get(net64)
        if fake_net is None:
            fake_net = self._pick_v6_net(net64)
        low = int(ip) & ((1 << 64) - 1)
        return str(ipaddress.IPv6Address((fake_net << 64) | low))

    def _pick_v6_net(self, net64: int) -> int:
        base = int(FAKE_V6_POOL.network_address) >> 64
        for attempt in range(MAX_DUMMY_ATTEMPTS):
            candidate = base | _keyed(self.salt, f"v6{net64}#{attempt}", 1 << 32)
            if candidate not in self._v6_used and candidate != net64:
                self._v6[net64] = candidate
                self._v6_used.add(candidate)
                return candidate
        raise MaskingError("no free stand-in IPv6 network could be generated")


def is_role_local(local: str) -> bool:
    """True when an e-mail local part is a generic role kept verbatim."""
    return local.lower() in ROLE_LOCAL_PARTS


def fake_local_part(salt: bytes, local: str) -> str:
    """A believable fictional local part preserving a dotted first.last shape."""
    first = FIRST_NAMES[_keyed(salt, "fl\x00" + local, len(FIRST_NAMES))].lower()
    if "." in local or "_" in local:
        last = LAST_NAMES[_keyed(salt, "ll\x00" + local, len(LAST_NAMES))].lower()
        sep = "." if "." in local else "_"
        return f"{first}{sep}{last}"
    return first


__all__ = [
    "FAKE_ZONE",
    "HostParts",
    "NetMapper",
    "decode_host",
    "fake_local_part",
    "in_fake_pool",
    "is_ipish",
    "is_role_local",
    "registrable_domain",
]
