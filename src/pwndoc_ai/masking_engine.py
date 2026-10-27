"""Forward replacement, stand-in generation and un-masking behind the ``Masker`` facade.

``MaskEngine`` holds one job's state: the client terms, the client domains, the per-instance salt,
the :class:`~pwndoc_ai.masking_net.NetMapper` and the :class:`~pwndoc_ai.masking_registry.Registry`.
Every public entry point of the facade takes the registry lock, so one Masker is thread-safe.
"""

from __future__ import annotations

import html
import ipaddress
import re
from collections.abc import Iterable
from functools import lru_cache

from . import masking_net as net
from . import masking_text as mt
from .masking_model import (
    GENERIC_WORDS,
    MAX_DUMMY_ATTEMPTS,
    DummyCollisionError,
    MappingKind,
    is_unmasked_ipv4,
)
from .masking_names import company_body, keyed_index, person_word, shape_of, slug_of
from .masking_registry import Entry, Registry

_WORD = re.compile(r"[^\W_]{4,}")
_PHONE = re.compile(r"^\+?[\d\s().\-/]{5,}$")
_FREEMAIL = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "icloud.com",
        "proton.me",
        "protonmail.com",
        "gmx.de",
        "web.de",
    }
)
# A last label that is a file extension makes ``brandarmy.php`` a file name, not a host.
_FILE_EXTS = frozenset(
    "php js mjs ts html htm aspx asp jsp json xml txt py sh exe dll zip gz tar pdf docx doc xlsx "
    "png jpg jpeg gif svg css yml yaml conf cfg log bak sql md java rb go cs ini war jar class "
    "cgi pl env lock old tmp swp".split()
)
NAME, HOST, EXACT = "name", "host", "exact"


@lru_cache(maxsize=1)
def _trusted_hosts() -> frozenset[str]:
    from .docx_engine.facts import TRUSTED_HOSTS

    return frozenset(TRUSTED_HOSTS)


def _name_restyle(original: str, observed: str) -> str:
    letters = [c for c in observed if c.isalpha()]
    if letters and all(c.isupper() for c in letters) and len(letters) > 1:
        return original.upper()
    if letters and all(c.islower() for c in letters):
        return original.lower()
    if original.islower() or original.isupper():
        return original.title()
    return original


class MaskEngine:
    """The mechanism; :class:`pwndoc_ai.masking.Masker` adds the public API on top."""

    _reg: Registry
    _salt: bytes
    _net: net.NetMapper
    _terms: tuple[str, ...]
    _term_kinds: dict[str, str]
    _canon: dict[str, str]
    _persons: dict[str, bool]
    _phones: frozenset[str]
    _client_domains: set[str]
    _term_words: frozenset[str]
    _real_keys: frozenset[str]
    _bodies: dict[str, str]
    _people: dict[str, str]
    _emails: dict[str, str]
    _pattern: re.Pattern[str]
    _inner: re.Pattern[str]
    _terms_re: re.Pattern[str] | None
    _client_terms_re: re.Pattern[str] | None

    # -- classification --------------------------------------------------------------------

    def _registrable_is_client(self, parts: net.HostParts) -> bool:
        reg = parts.registrable
        if reg in self._client_domains:
            return True
        if reg in _trusted_hosts() or reg.rsplit(".", 1)[-1] in _FILE_EXTS:
            return False
        label = reg.split(".")[0]
        return any(w == label or (len(w) >= 5 and w in label) for w in self._term_words)

    def _host_is_client(self, host: str) -> bool:
        parts = net.registrable_domain(host)
        if parts is None or net.decode_host(host) in _trusted_hosts():
            return False
        return self._registrable_is_client(parts)

    def _learn_host(self, host: str, explicit: bool) -> None:
        parts = net.registrable_domain(host)
        if parts is None or parts.registrable in _FREEMAIL:
            return
        if explicit or self._registrable_is_client(parts):
            if net.decode_host(host) not in _trusted_hosts():
                self._client_domains.add(parts.registrable)

    def _subdomain_has_term(self, sub: str) -> bool:
        return bool(sub) and self._terms_re is not None and bool(self._terms_re.search(sub))

    # -- forward pass ----------------------------------------------------------------------

    def _mask_text(self, text: str) -> str:
        if not text:
            return text
        with self._reg.lock:
            self._resolve_collisions(text)
            memo: dict[str, str] = {}  # per pass: no reassignment can happen mid-substitution
            return self._pattern.sub(lambda m: self._replace(m, memo), text)

    def _replace(self, m: re.Match[str], memo: dict[str, str] | None = None) -> str:
        name, value = m.lastgroup or "", m.group()
        if name == "url":
            return self._mask_url(value)
        if name == "email":
            return self._mask_email(value)
        if name == "ip4":
            return self._ip_dummy(self._net.fake_ipv4, value)
        if name == "ip6":
            return self._ip_dummy(self._net.fake_ipv6, value) if mt.is_masked_ipv6(value) else value
        if name == "host":
            return self._mask_host(value)
        if name == "term" and not mt.is_entity_name(m):
            if memo is None:
                return self._term_dummy(value)
            if value not in memo:
                memo[value] = self._term_dummy(value)
            return memo[value]
        return value

    def _mask_inner(self, text: str) -> str:
        """Mask a URL's userinfo/path/query: identifiers only, everything else byte-for-byte."""
        return self._inner.sub(self._replace, text) if text else text

    def _mask_url(self, matched: str) -> str:
        core, tail = mt.split_trailing(matched)
        parts = mt.split_authority(core)
        if parts is None:
            return matched
        prefix, host, suffix, _ = parts
        bracket = "[" if suffix.startswith("]") else ""
        if net.is_ipish(host):
            fake_fn = self._net.fake_ipv6 if ":" in host else self._net.fake_ipv4
            new_host = self._ip_dummy(fake_fn, host)
        else:
            new_host = self._mask_host(host)
        return self._mask_inner(prefix) + bracket + new_host + self._mask_inner(suffix) + tail

    def _mask_host(self, host: str) -> str:
        parts = net.registrable_domain(host)
        labels = host.split(".")
        if parts is None or net.decode_host(host) in _trusted_hosts():
            return self._term_subst(host)
        width = parts.registrable.count(".") + 1
        if len(labels) < width:
            return self._term_subst(host)
        sub, reg = ".".join(labels[:-width]), ".".join(labels[-width:])
        new_sub = self._term_subst(sub)
        if self._registrable_is_client(parts):
            hint = None if self._net.has_domain(parts.registrable) else self._slug_hint(parts)
            fake = self._net.fake_registrable(parts.registrable, hint)
            self._reg.add(fake, reg.lower(), MappingKind.DOMAIN, HOST, sent=True)
            new_reg = mt.style_like(fake, reg)
        else:
            new_reg = self._term_subst(reg)
        if not new_sub:
            return new_reg
        if new_reg != reg:
            self._reg.add(f"{new_sub}.{new_reg}", host.lower(), MappingKind.DOMAIN, HOST, True)
        return f"{new_sub}.{new_reg}"

    def _term_subst(self, text: str) -> str:
        if not text or self._terms_re is None:
            return text
        return self._terms_re.sub(
            lambda m: m.group() if mt.is_entity_name(m) else self._term_dummy(m.group()), text
        )

    def _email_is_client(self, local: str, domain: str) -> bool:
        """An address identifies the client by its domain or by a client word in its local part."""
        if self._host_is_client(domain):
            return True
        return self._terms_re is not None and bool(self._terms_re.search(local.replace(".", " ")))

    def _mask_email(self, address: str) -> str:
        cached = self._emails.get(address)
        if cached is not None:
            return cached
        local, _, domain = address.partition("@")
        if not self._email_is_client(local, domain):
            return address
        role = net.is_role_local(local)
        new_domain = self._mask_host(domain)
        new_local = local if role else self._fake_local(local, new_domain, address)
        dummy = f"{new_local}@{new_domain}"
        if dummy == address:
            return address
        self._reg.add(dummy, address, MappingKind.EMAIL, EXACT, sent=True)
        self._emails[address] = dummy
        return dummy

    def _fake_local(self, local: str, domain: str, address: str) -> str:
        """A fictional local part whose full address is not issued for another original."""
        for attempt in range(MAX_DUMMY_ATTEMPTS):
            candidate = net.fake_local_part(self._salt, f"{local.casefold()}#{attempt}")
            entry = self._reg.get(f"{candidate}@{domain}")
            if candidate.casefold() == local.casefold() or self._reg.is_reserved(candidate):
                continue
            if entry is None or entry.original == address:
                return candidate
        return "user" + str(keyed_index(self._salt, address, 0, 900000) + 100000)

    def _ip_dummy(self, fake_fn, value: str) -> str:
        fake = fake_fn(value)
        if fake is None:
            return value
        self._reg.add(fake, value, MappingKind.IP, EXACT, sent=True)
        return fake

    # -- names -----------------------------------------------------------------------------

    def _term_dummy(self, surface: str, sent: bool = True) -> str:
        original = html.unescape(surface)
        key = mt.norm(original)
        canon = self._canon.get(key, original)
        kind = MappingKind.for_term(self._term_kinds.get(key, "TERM"))
        if key in self._phones:
            dummy = self._phone_dummy(original)
            self._reg.add(dummy, original, kind, EXACT, sent)
            return dummy
        words = key.split(" ")
        if kind is MappingKind.AUDIT:
            dummy = self._audit_dummy(original, kind, sent)
        elif all(w in self._persons for w in words):
            dummy = mt.restyle(" ".join(self._person(w) for w in words), original)
        else:
            dummy = self._company_dummy(original, canon, kind, sent)
        self._reg.add(dummy, canon, kind, NAME, sent)
        return dummy

    def _audit_dummy(self, original: str, kind: MappingKind, sent: bool) -> str:
        """An audit name keeps its own words; only the client names inside it are swapped."""
        inner = self._client_terms_re
        swapped = inner.sub(lambda m: self._term_dummy(m.group(), sent), original) if inner else ""
        if swapped and swapped != original:
            return swapped
        return self._company_dummy(original, original, kind, sent)

    def _company_dummy(self, original: str, canon: str, kind: MappingKind, sent: bool) -> str:
        shape = shape_of(original)
        body_text = original[: len(original) - len(shape.tail)]
        canon_body = canon[: len(canon) - len(shape_of(canon).tail)] or canon
        body = self._company_body(body_text, shape.words, canon_body, kind)
        if sent and not shape.tail:
            self._reg.add(body, canon_body, kind, NAME, sent=True)
        return mt.restyle(body, body_text) + shape.tail

    def _company_body(self, body_text: str, words: int, canon: str, kind: MappingKind) -> str:
        """The canonical stand-in body of a company; registered as an (unsent) alias."""
        key = mt.norm(body_text)
        body = self._bodies.get(key)
        if body is None:
            body = company_body(self._salt, words, key, self._name_free)
            self._bodies[key] = body
            self._reg.add_name(body)
        self._reg.add(body, canon, kind, NAME, sent=False)
        return body

    def _person(self, word: str) -> str:
        dummy = self._people.get(word)
        if dummy is None:
            dummy = person_word(self._salt, word, self._persons[word], self._name_free)
            self._people[word] = dummy
            self._reg.add_name(dummy)
            canon = self._canon.get(word, word)
            self._reg.add(dummy, canon, MappingKind.CLIENT, NAME, sent=False)
        return dummy

    def _name_free(self, candidate: str) -> bool:
        if candidate.casefold() in GENERIC_WORDS:
            return False
        return self._reg.name_is_free(candidate, self._real_keys)

    def _phone_dummy(self, original: str) -> str:
        digits = iter(
            str(keyed_index(self._salt, f"{original}#{i}", 0, 10)) for i in range(len(original))
        )
        out = "".join(next(digits) if c.isdigit() else c for c in original)
        return out if out != original else out[:-1] + str((int(out[-1]) + 1) % 10)

    def _slug_hint(self, parts: net.HostParts) -> str | None:
        label = parts.registrable.split(".")[0]
        for term in sorted(self._terms, key=len, reverse=True):
            key = mt.norm(term)
            if self._term_kinds.get(key) == "AUDIT" or key in self._phones:
                continue
            if all(w in self._persons for w in key.split(" ")):
                continue
            words = [w for w in _WORD.findall(key) if w not in GENERIC_WORDS]
            if any(w == label or (len(w) >= 5 and w in label) for w in words):
                shape = shape_of(term)
                body_text = term[: len(term) - len(shape.tail)]
                kind = MappingKind.for_term(self._term_kinds.get(key))
                return slug_of(self._company_body(body_text, shape.words, body_text, kind))
        return None

    def _domain_free(self, candidate: str, hinted: bool) -> bool:
        if self._reg.is_reserved(candidate) or self._reg.get(candidate) is not None:
            return False
        slug = candidate.split(".")[0]
        if hinted:
            return True
        return not self._reg.is_reserved(slug) and self._name_free(slug.capitalize())

    # -- collisions ------------------------------------------------------------------------

    def _resolve_collisions(self, text: str) -> int:
        """Handle issued stand-ins that occur literally in new text; returns reassignments."""
        pattern = self._reg.pattern()
        if pattern is None:
            return 0
        reassigned = 0
        for m in pattern.finditer(text):
            entry = self._reg.get(m.group())
            if entry is None or self._masked_away(entry, text, m):
                continue
            if entry.sent:
                raise DummyCollisionError(
                    "the text already contains a stand-in value that was sent to the model"
                )
            self._reassign(mt.canonical_dummy(m.group()))
            reassigned += 1
        return reassigned

    @staticmethod
    def _masked_away(entry: Entry, text: str, m: re.Match[str]) -> bool:
        """An IP literal equal to a stand-in is itself masked, so it never reaches the model."""
        if entry.kind is not MappingKind.IP:
            return False
        for finder in (mt._IPV4_RE, mt._IPV6_RE):
            hit = finder.match(text, m.start())
            if hit is not None and hit.end() == m.end() and not is_unmasked_ipv4(hit.group()):
                return True
        return False

    def _reassign(self, key: str) -> None:
        self._reg.forget(key)
        for table in (self._bodies, self._people, self._emails):
            for real, dummy in tuple(table.items()):
                if dummy.casefold() == key:
                    del table[real]
        self._net.forget_domain(key)

    # -- unmask ----------------------------------------------------------------------------

    def _unmask_text(self, text: str) -> str:
        with self._reg.lock:
            pattern = self._reg.pattern()
            if pattern is None or not text:
                return text
            is_html = mt.looks_html(text)
            text = self._restore_strays(text)
            return pattern.sub(lambda m: self._restore(m, is_html), text)

    def _restore_strays(self, text: str) -> str:
        """Put back stand-ins the model derived itself: a new host octet in an issued fake
        ``/24`` or a new sub-domain label under an issued fake registrable domain. Both maps
        are one-to-one, so the real value is known exactly."""
        text = mt._IPV4_RE.sub(self._restore_stray_ip, text)
        issued = self._net.issued_domains()
        if not issued:
            return text
        sub_re = mt.subdomain_pattern(issued)

        def swap(m: re.Match[str]) -> str:
            if self._reg.get(m.group()) is not None:
                return m.group()
            real = issued[m.group("reg").casefold()]
            return m.group("sub") + mt.restyle_host(real, m.group("reg"))

        return sub_re.sub(swap, text)

    def _restore_stray_ip(self, m: re.Match[str]) -> str:
        if self._reg.get(m.group()) is not None:
            return m.group()
        return self._net.real_ipv4(m.group()) or m.group()

    def _restore(self, m: re.Match[str], is_html: bool) -> str:
        entry = self._reg.get(m.group())
        if entry is None:
            return m.group()
        observed = re.sub("%2e", ".", m.group(), flags=re.IGNORECASE)
        if entry.style == NAME:
            out = _name_restyle(entry.original, observed)
        elif entry.style == HOST:
            out = mt.restyle_host(entry.original, observed)
        else:
            out = entry.original
        out = mt.reencode(out, m.group())
        return mt.html_safe(out, is_html)

    def _scan(self, value: object) -> list[tuple[str, Entry]]:
        """Every issued stand-in surface present in a str/JSON value, with its entry."""
        found: list[tuple[str, Entry]] = []
        pattern = self._reg.pattern()
        if pattern is None:
            return found

        def visit(text: str) -> str:
            for m in pattern.finditer(text):
                entry = self._reg.get(m.group())
                if entry is not None:
                    found.append((m.group(), entry))
            return text

        mt.map_json(value, visit)
        return found

    # -- sensitive kinds / redaction -------------------------------------------------------

    def _match_kind(self, m: re.Match[str]) -> str | None:
        name, value = m.lastgroup or "", m.group()
        if name == "url":
            return (
                "URL" if self._url_is_client(html.unescape(mt.split_trailing(value)[0])) else None
            )
        if name == "ip4":
            return None if is_unmasked_ipv4(value) else "IP"
        if name == "ip6":
            return "IP" if mt.is_masked_ipv6(value) and self._net_v6_masked(value) else None
        if name == "email":
            local, _, domain = value.partition("@")
            return "EMAIL" if self._email_is_client(local, domain) else None
        if name == "host":
            return self._host_kind(value)
        if name == "term" and not mt.is_entity_name(m):
            return self._term_kinds.get(mt.norm(html.unescape(value)), "TERM")
        return None

    @staticmethod
    def _net_v6_masked(value: str) -> bool:
        ip = ipaddress.IPv6Address(value)
        return not (ip.is_loopback or ip.is_unspecified or ip.is_link_local or ip.is_multicast)

    def _host_kind(self, host: str) -> str | None:
        if self._host_is_client(host):
            return "DOMAIN"
        if self._terms_re is not None:
            hit = self._terms_re.search(host)
            if hit is not None:
                return self._term_kinds.get(mt.norm(hit.group()), "TERM")
        return None

    def _url_is_client(self, url: str) -> bool:
        for form in mt.decoded_forms(url):
            parts = mt.split_authority(form)
            host = parts[1] if parts else ""
            if host and net.is_ipish(host) and not is_unmasked_ipv4(host):
                return True
            if host and (self._host_is_client(host) or self._subdomain_has_term(host)):
                return True
            if self._url_names_client(form) or any(
                not is_unmasked_ipv4(ip.group()) for ip in mt._IPV4_RE.finditer(form)
            ):
                return True
        return False

    def _url_names_client(self, url: str) -> bool:
        folded = mt.norm(url)
        if any(mt.norm(term) in folded for term in self._terms):
            return True
        return any(word in folded for word in self._term_words)


def distinctive_words(terms: Iterable[str]) -> frozenset[str]:
    """Words (4+ letters, not generic) of company/extra terms, used to recognise client hosts."""
    return frozenset(
        word
        for term in terms
        for word in _WORD.findall(term.casefold())
        if word not in GENERIC_WORDS
    )


def is_phone(term: str) -> bool:
    return bool(_PHONE.match(term)) and sum(c.isdigit() for c in term) >= 5
