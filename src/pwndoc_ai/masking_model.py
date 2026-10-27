"""Shared vocabulary of the realistic-dummy masking (see docs/MASKING_DESIGN.md).

Small, dependency-free building blocks used by ``masking.py`` (facade), ``masking_names.py``,
``masking_net.py`` and ``masking_text.py``: the kinds of mapped values, the limits, the
reserved fictional zones and address pools, the immutable records that describe one mapping or
one leak, and the errors the call sites catch. Nothing here holds a mapping or a secret.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache

MIN_TERM_LEN = 3
MAX_TERM_LEN = 200
MAX_TERMS = 500
MAX_DEPTH = 100
MAX_DECODE_ROUNDS = 5
MAX_TRAILING_STRIP = 8
MAX_RESERVE_CHARS = 8_000_000
MAX_DUMMY_ATTEMPTS = 64
SALT_BYTES = 16

GENERIC_WORDS = frozenset({"corp", "inc", "ltd", "llc", "gmbh", "company", "group", "test"})

ROLE_LOCAL_PARTS = frozenset(
    {
        "admin",
        "administrator",
        "info",
        "support",
        "security",
        "noreply",
        "no-reply",
        "it",
        "hr",
        "sales",
        "contact",
        "help",
        "helpdesk",
        "billing",
        "abuse",
        "postmaster",
        "webmaster",
        "hostmaster",
        "office",
        "team",
        "root",
    }
)

FICTIONAL_ZONES = ("example", "test")

FAKE_PUBLIC_V4_POOLS = tuple(
    ipaddress.IPv4Network(net) for net in ("198.18.0.0/15", "100.64.0.0/10", "203.0.113.0/24")
)
FAKE_V6_POOL = ipaddress.IPv6Network("2001:db8::/32")
PRIVATE_V4_KINDS = tuple(
    ipaddress.IPv4Network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class MappingKind(str, Enum):
    """What a mapped value is; drives the dummy generator and ``sensitive_kinds``."""

    CLIENT = "CLIENT"
    AUDIT = "AUDIT"
    TERM = "TERM"
    DOMAIN = "DOMAIN"
    IP = "IP"
    EMAIL = "EMAIL"

    @classmethod
    def for_term(cls, kind: object) -> MappingKind:
        """The kind for a ``(kind, text)`` constructor pair; unknown kinds become TERM."""
        try:
            value = cls(str(kind).upper())
        except ValueError:
            return cls.TERM
        return value if value in (cls.CLIENT, cls.AUDIT, cls.TERM) else cls.TERM


class CollisionPolicy(str, Enum):
    """What ``mask`` does when new text literally contains an already-issued dummy."""

    REASSIGN = "reassign"
    REFUSE = "refuse"


@dataclass(frozen=True)
class Mapping:
    """One real value and its dummy (never logged: ``repr`` hides both strings)."""

    kind: MappingKind
    original: str
    dummy: str
    alias: bool = False

    def __repr__(self) -> str:
        return f"Mapping(kind={self.kind.value}, alias={self.alias})"


@dataclass(frozen=True)
class ReserveResult:
    """What ``Masker.reserve``/``prepare`` registered: counts only, never the text."""

    tokens: int
    reassigned: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class DummyLeak:
    """A dummy (or alias) still present after unmask, or an address from a fake pool."""

    kind: MappingKind
    dummy: str
    reason: str = "dummy"

    def __repr__(self) -> str:
        return f"DummyLeak(kind={self.kind.value}, reason={self.reason})"


class MaskingError(ValueError):
    """Base class: every masking failure is a ValueError so existing ``except`` clauses hold."""


class TooDeepError(MaskingError):
    """A structure nested at or beyond MAX_DEPTH: callers fail closed."""


class DummyCollisionError(MaskingError):
    """New text contains an issued dummy after that dummy was already sent to the model."""


class DummyLeakError(MaskingError):
    """Unmasked text still contains dummies; the answer must be rejected."""

    def __init__(self, leaks: tuple[DummyLeak, ...]) -> None:
        kinds = sorted({leak.kind.value for leak in leaks})
        super().__init__(f"the answer still contains {len(leaks)} stand-in value(s) ({kinds})")
        self.leaks = leaks


class OriginalLeakError(MaskingError):
    """Outgoing text still contains an original client value; nothing may be sent."""

    def __init__(self, kinds: tuple[str, ...]) -> None:
        super().__init__(f"client data would be sent unmasked ({', '.join(kinds)})")
        self.kinds = kinds


@lru_cache(maxsize=8192)
def canonical_ipv4(address: str) -> str:
    """``192.168.001.001`` -> ``192.168.1.1``; anything not dotted-quad is returned as is."""
    octets = address.split(".")
    if len(octets) != 4 or not all(o.isdigit() and 0 < len(o) <= 3 for o in octets):
        return address
    return ".".join(str(int(o)) for o in octets)


def pad_like(address: str, written: str) -> str:
    """Write ``address`` with the same per-octet zero padding as ``written``."""
    widths = [len(o) if o.startswith("0") and len(o) > 1 else 0 for o in written.split(".")]
    octets = address.split(".")
    if len(octets) != 4 or len(widths) != 4:
        return address
    return ".".join(o.zfill(w) for o, w in zip(octets, widths, strict=True))


@lru_cache(maxsize=8192)
def in_fake_pool(address: str) -> bool:
    """True for an address from a pool dummies are drawn from (never valid in a real answer)."""
    try:
        ip = ipaddress.ip_address(canonical_ipv4(address))
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        return ip in FAKE_V6_POOL
    return any(ip in pool for pool in FAKE_PUBLIC_V4_POOLS)


@lru_cache(maxsize=8192)
def is_unmasked_ipv4(address: str) -> bool:
    """True for IPv4 values that are deliberately left as written (not client-identifying)."""
    try:
        ip = ipaddress.IPv4Address(canonical_ipv4(address))
    except ValueError:
        return False
    return bool(
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip == ipaddress.IPv4Address("255.255.255.255")
    )


@lru_cache(maxsize=8192)
def private_kind(address: str) -> ipaddress.IPv4Network | None:
    """The RFC 1918 block an IPv4 address belongs to, or None for a public address."""
    try:
        ip = ipaddress.IPv4Address(canonical_ipv4(address))
    except ValueError:
        return None
    return next((net for net in PRIVATE_V4_KINDS if ip in net), None)


def is_fictional_host(host: str) -> bool:
    """True when ``host`` sits under a reserved zone (``.example``/``.test``/``example.*``)."""
    labels = host.lower().rstrip(".").split(".")
    if len(labels) < 2:
        return False
    return labels[-1] in FICTIONAL_ZONES or labels[-2] == "example"
