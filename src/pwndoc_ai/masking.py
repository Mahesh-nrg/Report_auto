"""Reversible masking of client-identifying data with realistic stand-ins.

``Masker.mask`` swaps sensitive values for believable fictional ones — a company name of the same
shape, fictional domains that keep their sub-domain labels, IPs that keep their class and ``/24``
topology, e-mails whose role local parts survive — so the model still reads the pattern of a
finding. ``Masker.unmask`` restores the real client values before anything is stored or sent to
PwnDoc. Endpoints, URL paths, query names, headers, cookies, tokens and PoC payloads pass through
byte-for-byte unless they literally contain a client identifier: a secret inside a payload is NOT
masked unless it equals a client term. One Masker belongs to one audit/job; its mapping lives only
in memory, is never logged (``repr`` shows counts only) and differs between instances through a
random per-instance salt.

See ``docs/MASKING_DESIGN.md`` for the full contract.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

from . import masking_net as net
from . import masking_text as mt
from .masking_engine import MaskEngine, distinctive_words, is_phone
from .masking_model import (
    GENERIC_WORDS,
    MAX_DEPTH,
    MAX_RESERVE_CHARS,
    MAX_TERM_LEN,
    MAX_TERMS,
    MIN_TERM_LEN,
    SALT_BYTES,
    DummyLeak,
    DummyLeakError,
    MappingKind,
    ReserveResult,
    TooDeepError,
    in_fake_pool,
)
from .masking_names import shape_of
from .masking_registry import Registry

log = logging.getLogger(__name__)

__all__ = [
    "MAX_DEPTH",
    "MAX_TERMS",
    "MAX_TERM_LEN",
    "MIN_TERM_LEN",
    "Masker",
    "NullMasker",
    "build_masker",
]

_EMAIL_TERM = re.compile(mt._EMAIL)
_HOST_TERM = re.compile(mt._HOST)
_PERSON_KINDS = {"FIRSTNAME": True, "LASTNAME": False}
_STEM_KINDS = frozenset({"CLIENT", "TERM", "COMPANY"})


def _clean_term(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    term = " ".join(value.split())
    if not MIN_TERM_LEN <= len(term) <= MAX_TERM_LEN or mt.is_object_id(term):
        return None
    return term


def _legal_stem(kind: str, term: str) -> str | None:
    """``Brandarmy Ltd`` -> ``Brandarmy``: the bare name is written in text just as often."""
    if kind not in _STEM_KINDS:
        return None
    shape = shape_of(term)
    stem = term[: len(term) - len(shape.tail)].strip(" ,")
    if not shape.tail or len(stem) < mt.LONG_TERM or stem.casefold() in GENERIC_WORDS:
        return None
    return stem


def _company_terms(company: Any) -> list[Any]:
    if isinstance(company, Mapping):
        return [company.get("name"), company.get("shortName")]
    return [company]


def _client_terms(client: Any) -> list[tuple[str, Any]]:
    if not isinstance(client, Mapping):
        return [("CLIENT", client)]
    first, last = client.get("firstname"), client.get("lastname")
    full = f"{first} {last}" if isinstance(first, str) and isinstance(last, str) else None
    contact = [("FIRSTNAME", first), ("LASTNAME", last), ("CLIENT", full)]
    contact += [("CLIENT", client.get(k)) for k in ("email", "phone", "cell")]
    return contact + [("CLIENT", t) for t in _company_terms(client.get("company"))]


def _scope_hosts(audit: Mapping[str, Any]) -> list[tuple[str, Any]]:
    hosts: list[tuple[str, Any]] = []
    for entry in audit.get("scope") or ():
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        if isinstance(name, str):
            parts = mt.split_authority(name) if "://" in name else None
            hosts.append(("HOST", parts[1] if parts else name))
        for host in entry.get("hosts") or ():
            if isinstance(host, Mapping):
                hosts.append(("HOST", host.get("hostname")))
    return hosts


def _audit_terms(audit: Any, extra_terms: Iterable[str] | str | None) -> list[tuple[str, Any]]:
    terms: list[tuple[str, Any]] = []
    if isinstance(audit, Mapping):
        terms += _client_terms(audit.get("client"))
        terms += [("CLIENT", t) for t in _company_terms(audit.get("company"))]
        terms.append(("AUDIT", audit.get("name")))
        terms += _scope_hosts(audit)
    if isinstance(extra_terms, str):
        extra_terms = [extra_terms]
    terms += [("TERM", t) for t in (extra_terms or ())]
    return terms


class Masker(MaskEngine):
    """Replace client-identifying values by realistic stand-ins and restore them. Thread-safe."""

    def __init__(self, terms: Iterable[tuple[str, str]] = ()) -> None:
        self._reg = Registry()
        self._salt = os.urandom(SALT_BYTES)
        self._net = net.NetMapper(
            self._salt, is_free=self._domain_free, prefix_blocked=self._reg.prefix_reserved
        )
        self._client_domains: set[str] = set()
        self._bodies: dict[str, str] = {}
        self._people: dict[str, str] = {}
        self._emails: dict[str, str] = {}
        self._setup_terms(self._canonical(terms))

    @staticmethod
    def _canonical(terms: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
        seen: dict[str, tuple[str, str]] = {}
        for kind, raw in terms:
            term = _clean_term(raw)
            if term is None:
                continue
            seen.setdefault(mt.norm(term), (str(kind).upper(), term))
        for kind, term in tuple(seen.values()):  # explicit terms keep their own kind
            stem = _legal_stem(kind, term)
            if stem is not None:
                seen.setdefault(mt.norm(stem), (kind, stem))
        if len(seen) > MAX_TERMS:
            log.warning("masking: %d terms given, only the first %d are used", len(seen), MAX_TERMS)
        return list(seen.values())[:MAX_TERMS]

    def _setup_terms(self, canonical: list[tuple[str, str]]) -> None:
        plain: list[tuple[str, str]] = []
        self._persons: dict[str, bool] = {}
        for kind, term in canonical:
            if _EMAIL_TERM.fullmatch(term):
                self._learn_host(term.rsplit("@", 1)[1], explicit=True)
                continue
            if kind == "HOST" or (" " not in term and _HOST_TERM.fullmatch(term)):
                self._learn_host(term, explicit=True)
                continue
            if kind in _PERSON_KINDS and " " not in term:
                self._persons.setdefault(mt.norm(term), _PERSON_KINDS[kind])
            plain.append(("CLIENT" if kind in _PERSON_KINDS else kind, term))
        self._terms = tuple(term for _, term in plain)
        self._term_kinds = {mt.norm(term): kind for kind, term in plain}
        self._canon = {mt.norm(term): term for _, term in plain}
        self._phones = frozenset(mt.norm(t) for t in self._terms if is_phone(t))
        company = [t for k, t in plain if k != "AUDIT" and mt.norm(t) not in self._persons]
        self._term_words = distinctive_words(t for t in company if not is_phone(t))
        self._real_keys = frozenset((*self._term_kinds, *self._term_words, *self._persons))
        self._pattern = mt.build_pattern(self._terms)
        self._inner = mt.build_pattern(self._terms, with_url=False)
        self._terms_re = mt.term_pattern(self._terms)
        self._client_terms_re = mt.term_pattern(t for k, t in plain if k != "AUDIT")

    # -- public, unchanged signatures ------------------------------------------------------

    @classmethod
    def from_audit(cls, audit: Mapping[str, Any], extra_terms: Iterable[str] = ()) -> Masker:
        """Masker for one PwnDoc audit (prepared with it); odd shapes never raise."""
        masker = cls(_audit_terms(audit, extra_terms))
        if isinstance(audit, Mapping):
            masker.prepare(audit)
        return masker

    @property
    def enabled(self) -> bool:
        return True

    def __repr__(self) -> str:  # never show the mapping: it is the secret
        return f"Masker(terms={len(self._terms)}, mappings={len(self._reg)})"

    def mask(self, text: str) -> str:
        """Text with every client value replaced by a stand-in (non-str/empty: unchanged)."""
        if not isinstance(text, str) or not text:
            return text
        if "<" not in text:
            return self._mask_text(text)
        with self._reg.lock:
            self._resolve_collisions(text)
            return mt.map_segments(
                mt._TAG, text, lambda m: self._mask_tag(m.group()), self._mask_text
            )

    def mask_plain(self, text: str) -> str:
        """Mask plain text (titles, references, PoC): ``<...>`` is NOT treated as markup."""
        if not isinstance(text, str) or not text:
            return text
        return self._mask_text(text)

    def mask_json(self, value: Any) -> Any:
        """New structure with every string masked; keys and non-strings kept (fails closed deep)."""
        return mt.map_json(value, self.mask)

    def mask_json_plain(self, value: Any) -> Any:
        """Like :meth:`mask_json` for plain-text values."""
        return mt.map_json(value, self.mask_plain)

    def unmask(self, value: Any) -> Any:
        """Restore stand-ins in str/dict/list/tuple (single pass; raises past MAX_DEPTH)."""
        try:
            return mt.map_json(value, self._unmask_text)
        except TooDeepError:
            raise
        except Exception:  # noqa: BLE001 - an unreadable leaf stays as is; leak checks catch it
            log.exception("unmask failed; returning the value unchanged")
        return value

    def leftovers(self, value: Any) -> list[str]:
        """Legacy ``[[KIND_n]]`` strays plus any issued stand-in still present, deduplicated."""
        found: list[str] = []

        def scan(text: str) -> str:
            found.extend(m.group() for m in mt._LEGACY_PH.finditer(text))
            return text

        mt.map_json(value, scan)
        return list(dict.fromkeys([*found, *self.dummies_in(value)]))

    def sensitive_kinds(self, text: str) -> list[str]:
        """Kinds of client data in plain ``text`` (CLIENT/IP/EMAIL/URL/DOMAIN...), no masking."""
        if not isinstance(text, str) or not text:
            return []
        kinds = (self._match_kind(m) for m in self._pattern.finditer(text))
        return list(dict.fromkeys(k for k in kinds if k is not None))

    # -- new API ---------------------------------------------------------------------------

    def reserve(self, value: Any) -> ReserveResult:
        """Register corpus text so no stand-in ever equals it; learn client hosts from it."""
        counts = {"tokens": 0, "reassigned": 0, "seen": 0, "truncated": False}

        def visit(text: str) -> str:
            if counts["seen"] >= MAX_RESERVE_CHARS:
                counts["truncated"] = True
                return text
            counts["seen"] += len(text)
            with self._reg.lock:
                counts["tokens"] += self._reg.reserve_text(text)
                for m in mt._HOST_RE.finditer(text):
                    self._learn_host(m.group(), explicit=False)
                counts["reassigned"] += self._reassign_unsent(text)
            return text

        try:
            mt.map_json(value, visit)
        except TooDeepError:
            counts["truncated"] = True
        return ReserveResult(
            tokens=int(counts["tokens"]),
            reassigned=int(counts["reassigned"]),
            truncated=bool(counts["truncated"]),
        )

    def prepare(self, corpus: Any) -> ReserveResult:
        """``reserve`` over a whole document/audit corpus (str, JSON or iterable of strings)."""
        if corpus is None or isinstance(corpus, (str, Mapping, list, tuple)):
            return self.reserve(corpus)
        return self.reserve(list(corpus))

    def redact(self, text: str) -> str:
        """Client values REMOVED (no stand-ins) — for text that leaves the machine (web queries)."""
        if not isinstance(text, str) or not text:
            return text
        cleaned = self._pattern.sub(
            lambda m: " " if self._match_kind(m) is not None else m.group(), text
        )
        return " ".join(cleaned.split())

    def originals_in(self, value: Any) -> list[str]:
        """The real client values behind the stand-ins present in ``value``."""
        with self._reg.lock:
            return list(dict.fromkeys(entry.original for _, entry in self._scan(value)))

    def dummies_in(self, value: Any) -> list[str]:
        """Issued stand-in surfaces (and aliases) still present in ``value``."""
        with self._reg.lock:
            return list(dict.fromkeys(surface for surface, _ in self._scan(value)))

    def dummy_leaks(self, value: Any) -> list[DummyLeak]:
        """Stand-ins still present plus any fake-pool address, each typed by kind."""
        with self._reg.lock:
            leaks = {s: DummyLeak(e.kind, s, "dummy") for s, e in self._scan(value)}

            def visit(text: str) -> str:
                for address in self._addresses(text):
                    if address not in leaks and self._is_stray_pool_address(address):
                        leaks[address] = DummyLeak(MappingKind.IP, address, "fake-pool")
                for host in self._fake_zone_hosts(text):
                    if host not in leaks:
                        leaks[host] = DummyLeak(MappingKind.DOMAIN, host, "fake-zone")
                return text

            mt.map_json(value, visit)
        return list(leaks.values())

    def assert_no_dummies(self, value: Any) -> None:
        """Raise ``DummyLeakError`` if any stand-in or fake-pool address remains in ``value``."""
        leaks = self.dummy_leaks(value)
        if leaks:
            raise DummyLeakError(tuple(leaks))

    # -- helpers ---------------------------------------------------------------------------

    @staticmethod
    def _addresses(text: str) -> list[str]:
        return [m.group() for m in mt._IPV4_RE.finditer(text)] + mt.ipv6_candidates(text)

    def _fake_zone_hosts(self, text: str) -> list[str]:
        """``.example`` hosts that look like a mangled issued stand-in domain (same registrable
        label prefix) and are not reserved corpus text. Other ``.example`` hosts the model
        writes on its own (``attacker.example``, RFC 2606 style) are legitimate text."""
        labels = self._issued_zone_labels()
        if not labels:
            return []
        found = []
        for m in mt._HOST_RE.finditer(text):
            parts = m.group().lower().split(".")
            if len(parts) < 2 or parts[-1] != net.FAKE_ZONE or self._reg.is_reserved(m.group()):
                continue
            if any(_resembles(parts[-2], label) for label in labels):
                found.append(m.group())
        return found

    def _issued_zone_labels(self) -> frozenset[str]:
        suffix = "." + net.FAKE_ZONE
        return frozenset(
            surface[: -len(suffix)].rsplit(".", 1)[-1].rsplit("@", 1)[-1]
            for surface, _ in self._reg.items()
            if surface.endswith(suffix)
        )

    def _is_stray_pool_address(self, address: str) -> bool:
        if self._net.issued_v4_prefix(address) and self._reg.get(address) is None:
            return not self._reg.is_reserved(address)
        if not in_fake_pool(address):
            return False
        return not (self._reg.known_original(address) or self._reg.is_reserved(address))

    def _reassign_unsent(self, text: str) -> int:
        try:
            return self._resolve_collisions(text)
        except ValueError:
            return 0  # a sent stand-in in the corpus: a later mask() of that text refuses

    def _mask_tag(self, tag: str) -> str:
        return mt.map_segments(mt._ATTR, tag, self._mask_attr, lambda text: text)

    def _mask_attr(self, m: re.Match[str]) -> str:
        quote, value = next(
            (q, m.group(g))
            for g, q in (("dq", '"'), ("sq", "'"), ("bare", ""))
            if m.group(g) is not None
        )
        return f"{m.group('pre')}{quote}{self._pattern.sub(self._replace, value)}{quote}"


class NullMasker:
    """Same interface as Masker, acting as identity (masking switched off)."""

    @property
    def enabled(self) -> bool:
        return False

    def mask(self, text: str) -> str:
        return text

    def mask_plain(self, text: str) -> str:
        return text

    def mask_json(self, value: Any) -> Any:
        return value

    def mask_json_plain(self, value: Any) -> Any:
        return value

    def unmask(self, value: Any) -> Any:
        return value

    def leftovers(self, value: Any) -> list[str]:
        return []

    def sensitive_kinds(self, text: str) -> list[str]:
        return []

    def reserve(self, value: Any) -> ReserveResult:
        return ReserveResult(tokens=0)

    def prepare(self, corpus: Any) -> ReserveResult:
        return ReserveResult(tokens=0)

    def redact(self, text: str) -> str:
        return text

    def originals_in(self, value: Any) -> list[str]:
        return []

    def dummies_in(self, value: Any) -> list[str]:
        return []

    def dummy_leaks(self, value: Any) -> list[DummyLeak]:
        return []

    def assert_no_dummies(self, value: Any) -> None:
        return None


def build_masker(audit: Mapping[str, Any], enabled: bool) -> Masker | NullMasker:
    """A Masker for the audit when ``enabled`` (settings.mask_sensitive), else a NullMasker."""
    return Masker.from_audit(audit) if enabled else NullMasker()


_MANGLE_PREFIX = 5  # shared leading letters that make a fake-zone label a mangled stand-in


def _resembles(label: str, issued: str) -> bool:
    if label == issued or issued in label or label in issued:
        return True
    size = min(_MANGLE_PREFIX, len(label), len(issued))
    return size >= _MANGLE_PREFIX and label[:size] == issued[:size]
