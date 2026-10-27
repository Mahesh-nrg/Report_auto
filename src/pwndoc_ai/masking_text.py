"""Regex assembly, HTML/tag-aware segmenting and the unmask compiler.

Pure, registry-free helpers the :class:`~pwndoc_ai.masking.Masker` facade drives. The master
pattern matches, in priority order, legacy ``[[LIT]]`` look-alikes, URLs, e-mails, IPv4/IPv6,
client host names and known terms; :func:`compile_unmask` builds the single reverse alternation.
"""

from __future__ import annotations

import html
import ipaddress
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import unquote

from .masking_model import MAX_DECODE_ROUNDS, MAX_DEPTH, MAX_TRAILING_STRIP, TooDeepError

_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{24}$")
_WS = r"(?:\s|&nbsp;)"
_WS_RUN = re.compile(r"(?:\s|&nbsp;)+", re.IGNORECASE)

# Legacy [[KIND_n]] still accepted by unmask (never emitted); and look-alikes to escape.
_PH_BODY = r"\[\[\s{0,4}%s\s{0,4}\\?_\s{0,4}%s\s{0,4}\]\]"
_LEGACY_PH = re.compile(_PH_BODY % (r"(?P<kind>[A-Za-z]{1,16})", r"(?P<num>[0-9]{1,9})"))

_URL = r"https?://[^\s<>\"'`&]+(?:&(?!(?:quot|lt|gt|apos|#39|#x27);)[^\s<>\"'`&]*)*"
_EMAIL = (
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}"
    r"(?:\.[A-Za-z0-9-]{1,63}){1,10}"
)
# Leading zeros (``192.168.001.001``) are the same host as written by many scanners.
_OCTET = r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)"
_IPV4 = rf"(?<!\w)(?<![0-9]\.)(?:{_OCTET}\.){{3}}{_OCTET}(?!\w|\.[0-9]|-[A-Za-z])"
_IPV4_RE = re.compile(_IPV4)
_IPV6 = r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:]|\.[0-9])"
_LABEL = r"[^\W_](?:[\w-]{0,61}[^\W_])?"
_HOST = rf"(?<![\w.@-])(?:{_LABEL}\.){{1,10}}(?:xn--[\w-]{{1,59}}|[^\W\d_]{{2,24}})(?![\w-]|\.\w)"
_HOST_RE = re.compile(_HOST)
_IPV6_RE = re.compile(_IPV6)
_TAG = re.compile(r"</?[A-Za-z][^<>]*>|<![A-Za-z][^<>]*>")

_ATTR = re.compile(
    r"""(?<![\w:.-])(?P<pre>(?P<name>href|alt|title)\s*=\s*)"""
    r"""(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\s"'>]+))""",
    re.IGNORECASE,
)
_ENTITY_PREFIXES = ("&#x", "&#", "&")
_TRAILING = ".,;:!?"
_BRACKETS = {")": "(", "]": "["}

_ESCAPED_FORMS = {
    "&": "(?:&|&amp;|&#38;|&#x26;)",
    "<": "(?:<|&lt;|&#60;|&#x3c;)",
    ">": "(?:>|&gt;|&#62;|&#x3e;)",
    '"': '(?:"|&quot;|&#34;|&#x22;)',
    "'": "(?:'|&#39;|&#x27;|&apos;)",
}


def is_object_id(text: str) -> bool:
    return bool(_OBJECT_ID.match(text))


def norm(text: str) -> str:
    return _WS_RUN.sub(" ", text).casefold()


def _escape_char(char: str) -> str:
    return _ESCAPED_FORMS.get(char) or re.escape(char)


def _term_pattern(term: str) -> str:
    parts = ("".join(_escape_char(c) for c in part) for part in term.split(" "))
    return _WS_RUN.pattern.join(parts)


_LETTER = r"[^\W\d_]"
# A term may carry a plural/possessive ``s`` (``Brandarmys``); the stem alone is replaced.
_PLURAL_END = r"(?=(?:s(?!{0}|\.\w))?(?!{0}))"
# Terms of 4+ characters treat ``_`` and digits as separators (``brandarmy_lang``).
LONG_TERM = 4


def _terms_alternation(terms: Iterable[str]) -> str | None:
    """Boundary-aware alternation of ``terms``, longest first (letter boundary for long ones)."""
    ordered = sorted(terms, key=lambda t: (-len(t), t))
    long_terms = [t for t in ordered if len(t) >= LONG_TERM]
    short_terms = [t for t in ordered if len(t) < LONG_TERM]
    parts = []
    if long_terms:
        alt = "|".join(_term_pattern(t) for t in long_terms)
        parts.append(rf"(?<!{_LETTER})(?:{alt}){_PLURAL_END.format(_LETTER)}")
    if short_terms:
        alt = "|".join(_term_pattern(t) for t in short_terms)
        parts.append(rf"(?<!\w)(?:{alt}){_PLURAL_END.format(chr(92) + 'w')}")
    return "|".join(parts) or None


def build_pattern(terms: Iterable[str], with_url: bool = True) -> re.Pattern[str]:
    """The master forward pattern: url | email | ipv4 | ipv6 | host | term (first wins).

    ``with_url=False`` is the pattern for the inside of a URL (userinfo, path, query).
    """
    parts = [f"(?P<url>{_URL})"] if with_url else []
    parts += [
        f"(?P<email>{_EMAIL})",
        f"(?P<ip4>{_IPV4})",
        f"(?P<ip6>{_IPV6})",
        f"(?P<host>{_HOST})",
    ]
    alternatives = _terms_alternation(terms)
    if alternatives:
        parts.append(f"(?P<term>{alternatives})")
    return re.compile("|".join(parts), re.IGNORECASE)


def term_pattern(terms: Iterable[str]) -> re.Pattern[str] | None:
    """Terms only (for URL paths, payloads and sub-domain labels), longest first."""
    alternatives = _terms_alternation(terms)
    return re.compile(alternatives, re.IGNORECASE) if alternatives else None


def subdomain_pattern(registrables: Iterable[str]) -> re.Pattern[str]:
    """Hosts with at least one sub-domain label under one of ``registrables``."""
    alt = "|".join(re.escape(r) for r in sorted(registrables, key=lambda r: (-len(r), r)))
    return re.compile(
        rf"(?<![\w.@-])(?P<sub>(?:{_LABEL}\.){{1,10}})(?P<reg>{alt})(?![\w-]|\.\w)",
        re.IGNORECASE,
    )


def is_masked_ipv6(text: str) -> bool:
    if sum(1 for group in text.split(":") if group) < 2:
        return False
    try:
        ipaddress.IPv6Address(text)
    except ValueError:
        return False
    return True


def is_entity_name(m: re.Match[str]) -> bool:
    text = m.string
    if text[m.end() : m.end() + 1] != ";":
        return False
    before = text[max(0, m.start() - 3) : m.start()].lower()
    return before.endswith(_ENTITY_PREFIXES)


def split_trailing(url: str) -> tuple[str, str]:
    """Split sentence punctuation / an unbalanced closing bracket off a URL (bounded strip)."""
    end = len(url)
    limit = max(0, len(url) - MAX_TRAILING_STRIP)
    while end > limit:
        ch = url[end - 1]
        if ch in _BRACKETS:
            window = url[limit:end]
            if window.count(ch) <= window.count(_BRACKETS[ch]):
                break
        elif ch not in _TRAILING:
            break
        end -= 1
    return url[:end], url[end:]


def decoded_forms(url: str) -> list[str]:
    forms = [url]
    current = url
    for _ in range(MAX_DECODE_ROUNDS):
        decoded = unicodedata.normalize("NFKC", unquote(current))
        if decoded == current:
            break
        forms.append(decoded)
        current = decoded
    forms.append(unicodedata.normalize("NFKC", url))
    return list(dict.fromkeys(forms))


def split_authority(url: str) -> tuple[str, str, str, str] | None:
    """``(prefix, host, suffix_after_host, userinfo_at)`` for a URL, or None if hostless.

    ``prefix`` is scheme + ``//`` + userinfo; ``host`` is the bare host (no port); ``suffix`` is
    ``:port`` plus path/query/fragment. Rebuild as ``prefix + newhost + suffix``.
    """
    m = re.match(r"^(?P<scheme>https?://)(?P<auth>[^/?#]*)(?P<rest>[/?#].*)?$", url, re.IGNORECASE)
    if m is None or not m.group("auth"):
        return None
    auth = m.group("auth")
    userinfo, _, hostport = auth.rpartition("@")
    user_at = f"{userinfo}@" if "@" in auth else ""
    if hostport.startswith("["):  # IPv6 literal
        host, _, port = hostport[1:].partition("]")
        port = "]" + port
    else:
        host, sep, rest = hostport.partition(":")
        port = (sep + rest) if sep else ""
    prefix = m.group("scheme") + user_at
    suffix = port + (m.group("rest") or "")
    return prefix, host, suffix, user_at


def map_segments(
    pattern: re.Pattern[str],
    text: str,
    on_match: Callable[[re.Match[str]], str],
    on_gap: Callable[[str], str],
) -> str:
    out: list[str] = []
    pos = 0
    for m in pattern.finditer(text):
        out.append(on_gap(text[pos : m.start()]))
        out.append(on_match(m))
        pos = m.end()
    out.append(on_gap(text[pos:]))
    return "".join(out)


def map_json(value: Any, fn: Callable[[str], str], depth: int = 0) -> Any:
    if isinstance(value, str):
        return fn(value)
    if not isinstance(value, Mapping | list | tuple):
        return value
    if depth >= MAX_DEPTH:
        raise TooDeepError(f"structure is nested too deeply (more than {MAX_DEPTH} levels)")
    if isinstance(value, Mapping):
        return {k: map_json(v, fn, depth + 1) for k, v in value.items()}
    return [map_json(v, fn, depth + 1) for v in value]


# -- unmask ---------------------------------------------------------------------------------

_DOT = r"(?:\.|%2[eE])"
_BEFORE = r"(?:(?<![A-Za-z0-9])(?<![0-9]\.)|(?<=%2[eE]))"
_AFTER = r"(?![A-Za-z0-9]|\.[0-9])"
# Name-like stand-ins: digits and ``_`` separate (``Oakhurst2026``), a plural ``s`` may follow.
_BEFORE_WORD = r"(?:(?<![A-Za-z])|(?<=%2[eE]))"
_AFTER_WORD = r"(?=(?:s(?![A-Za-z]|\.\w))?(?![A-Za-z]|\.[0-9]))"
_HTML_SPECIAL = re.compile(r"[&<>]")
_LOOKS_HTML = re.compile(r"</?[A-Za-z][^<>]{0,200}>|&(?:amp|lt|gt|quot|nbsp|#\d{1,7});")


def compile_unmask(dummies: Iterable[str]) -> re.Pattern[str] | None:
    """One case-insensitive alternation of all dummy/alias surfaces, longest-first.

    A dot may also appear URL-encoded (``%2e``) so an encoded stand-in host still restores.
    """
    ordered = sorted({d for d in dummies if d}, key=lambda d: (-len(d), d))
    if not ordered:
        return None
    # Grouped by the start boundary (letter vs other first character: the groups can never
    # match at the same position), so each lookbehind runs once per position, not per dummy.
    groups: dict[str, list[str]] = {}
    for dummy in ordered:
        before = _BEFORE_WORD if dummy[:1].isalpha() else _BEFORE
        body = re.escape(dummy).replace("\\.", _DOT)
        groups.setdefault(before, []).append(body + _unmask_after(dummy))
    alt = "|".join(f"{before}(?:{'|'.join(bodies)})" for before, bodies in groups.items())
    return re.compile(alt, re.IGNORECASE)


def _unmask_after(dummy: str) -> str:
    if dummy[-1:].isalpha():
        return _AFTER_WORD
    return _AFTER + "(?!-[A-Za-z])" if dummy[-1:].isdigit() else _AFTER


def canonical_dummy(matched: str) -> str:
    """The registry key of a matched stand-in (case-folded, ``%2e`` decoded)."""
    return re.sub("%2e", ".", matched, flags=re.IGNORECASE).casefold()


def reencode(original: str, matched: str) -> str:
    """Re-apply the ``%2e`` dot encoding the model used on ``matched`` to ``original``."""
    m = re.search("%2e", matched, re.IGNORECASE)
    return original.replace(".", m.group()) if m else original


def restyle(original: str, observed: str) -> str:
    """Restore ``original`` in the casing style the model used for ``observed``."""
    letters = [c for c in observed if c.isalpha()]
    if letters and all(c.isupper() for c in letters) and len(letters) > 1:
        return original.upper()
    if letters and all(c.islower() for c in letters):
        return original.lower()
    return original


def restyle_host(original: str, observed: str) -> str:
    """Casing of a host: UPPER/lower as observed, else capitalise labels the model capitalised."""
    styled = restyle(original, observed)
    if styled != original or original != original.lower():
        return styled
    seen = observed.replace("%2e", ".").replace("%2E", ".").split(".")
    labels = original.split(".")
    if len(seen) != len(labels):
        return original
    return ".".join(
        label.capitalize() if look[:1].isupper() else label
        for label, look in zip(labels, seen, strict=True)
    )


def style_like(fake: str, real: str) -> str:
    """Apply the per-label casing of a real host to a fake one of the same label count."""
    fake_labels, real_labels = fake.split("."), real.split(".")
    if len(fake_labels) != len(real_labels):
        return restyle(fake, real)
    out = []
    for label, look in zip(fake_labels, real_labels, strict=True):
        if look.isupper() and len(look) > 1:
            out.append(label.upper())
        else:
            out.append(label.capitalize() if look[:1].isupper() else label.lower())
    return ".".join(out)


def looks_html(text: str) -> bool:
    """True when ``text`` carries a tag or an HTML entity (so restored values are escaped)."""
    return bool(_LOOKS_HTML.search(text))


def html_safe(original: str, is_html: bool) -> str:
    """Escape ``& < >`` of a restored value when the surrounding answer is HTML."""
    if is_html and _HTML_SPECIAL.search(original):
        return html.escape(original, quote=False)
    return original


def ipv6_candidates(text: str) -> list[str]:
    """Valid IPv6 literals in ``text`` (bounded pattern, then ``ipaddress`` validation)."""
    return [m.group() for m in _IPV6_RE.finditer(text) if is_masked_ipv6(m.group())]


__all__ = [
    "build_pattern",
    "canonical_dummy",
    "compile_unmask",
    "decoded_forms",
    "html_safe",
    "ipv6_candidates",
    "is_entity_name",
    "is_masked_ipv6",
    "is_object_id",
    "looks_html",
    "map_json",
    "map_segments",
    "norm",
    "reencode",
    "restyle",
    "restyle_host",
    "split_authority",
    "split_trailing",
    "style_like",
    "term_pattern",
]
