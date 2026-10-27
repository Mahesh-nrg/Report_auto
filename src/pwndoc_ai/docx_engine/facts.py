"""Facts a paragraph carries that an edit must not lose or invent (addresses, links, ids)."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlparse

_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w]|\.\d)")
_IPV6 = re.compile(r"(?<![\w:])[0-9A-Fa-f:]{2,39}:[0-9A-Fa-f:]{0,39}(?![\w:])")
_URL = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+")
_CVE = re.compile(r"CVE-\d{4}-\d{4,7}", re.IGNORECASE)
_CWE = re.compile(r"CWE-\d+", re.IGNORECASE)
_NUMBER = re.compile(r"(?<!\w)\d+(?:[.,]\d+)*(?!\w)")
_TAG = re.compile(r"</?[A-Za-z][^<>\s/]*(?:\s[^<>]*)?/?>|<[?!][^<>]*>")
_REFERENCE = re.compile(
    r"(?:CVE|CWE|CAPEC)-[\d-]+|(?<![\w])A\d{2}[:_-]\d{4}(?![\w])|(?<![\w])T\d{4}(?:\.\d{3})?(?![\w])",
    re.IGNORECASE,
)
_STRIP = ".,;:!?\"'"
_CODE_CHARS = frozenset("/\\_=<>{}[]$|~^*#@`")
_FILE_NAME = re.compile(r"\w\.[a-z0-9]{2,5}$")
_ORDINAL = re.compile(r"^\d+(?:st|nd|rd|th)$", re.IGNORECASE)
_MIXED = re.compile(r"^(?=\w*\d)(?=\w*[A-Za-z])\w{3,}$")
_URL_TRAIL = ".,;:!?)]}'\""
TRUSTED_HOSTS = frozenset(
    {
        "cwe.mitre.org",
        "cve.mitre.org",
        "www.cve.org",
        "cve.org",
        "nvd.nist.gov",
        "owasp.org",
        "www.owasp.org",
        "cheatsheetseries.owasp.org",
        "attack.mitre.org",
        "capec.mitre.org",
        "www.first.org",
        "csrc.nist.gov",
    }
)


@dataclass(frozen=True)
class Facts:
    """What a text states: ``strict`` must stay as it is; ``code`` tokens must only be kept."""

    addresses: frozenset[str]  # IPv4 / IPv6
    emails: frozenset[str]
    urls: frozenset[str]
    ids: frozenset[str]  # CVE and CWE, upper-cased
    numbers: frozenset[str]
    code: frozenset[str]
    tags: frozenset[str]


def _urls(text: str) -> frozenset[str]:
    return frozenset(m.group().rstrip(_URL_TRAIL) for m in _URL.finditer(text))


def _addresses(text: str) -> frozenset[str]:
    found = {m.group() for m in _IPV4.finditer(text)}
    for match in _IPV6.finditer(text):
        try:
            ipaddress.IPv6Address(match.group())
        except ValueError:
            continue
        found.add(match.group())
    return frozenset(found)


def _is_code(token: str) -> bool:
    token = token.strip(_STRIP)
    if len(token) < 2 or _ORDINAL.match(token):
        return False
    return (
        any(char in _CODE_CHARS for char in token)
        or _FILE_NAME.search(token) is not None
        or _MIXED.match(token) is not None
    )


def extract_facts(text: str) -> Facts:
    return Facts(
        addresses=_addresses(text),
        emails=frozenset(m.group().lower() for m in _EMAIL.finditer(text)),
        urls=_urls(text),
        ids=frozenset(m.group().upper() for m in _CVE.finditer(text))
        | frozenset(m.group().upper() for m in _CWE.finditer(text)),
        numbers=frozenset(m.group() for m in _NUMBER.finditer(text)),
        code=frozenset(t.strip(_STRIP) for t in text.split() if _is_code(t)),
        tags=frozenset(m.group().lower() for m in _TAG.finditer(text)),
    )


def reference_numbers(text: str) -> frozenset[str]:
    """Numbers that only name a public reference: CVE/CWE/CAPEC ids, OWASP categories (A03:2021),
    ATT&CK technique ids and the digits of a link to a trusted reference site."""
    spans = [m.group() for m in _REFERENCE.finditer(text)]
    spans += [u for u in _urls(text) if url_host(u) in TRUSTED_HOSTS]
    return frozenset(m.group() for span in spans for m in _NUMBER.finditer(span))


def url_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
