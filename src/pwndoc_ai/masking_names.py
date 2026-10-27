"""Shape-preserving fictional company names for the realistic-dummy masking.

A client/company/audit term becomes an invented company name of the same shape: the legal
suffix (``Inc``, ``GmbH``...) is kept verbatim, the word count of the rest is matched, and the
casing style (UPPER, lower, Title, mixed) is applied to the dummy. Pools are embedded and hold
invented words only (no well-known brands). The pick is a keyed hash of the normalised original
under a per-instance salt, so it is stable inside one Masker and differs between Maskers.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass

from .masking_model import GENERIC_WORDS, MAX_DUMMY_ATTEMPTS, MaskingError

ROOTS = (
    "Lumenfold",
    "Northgate",
    "Verdania",
    "Kestrelon",
    "Brightwell",
    "Corvantis",
    "Halvorne",
    "Quillmark",
    "Tessaline",
    "Ambervale",
    "Silvercrest",
    "Draymoor",
    "Fenwicke",
    "Orlantic",
    "Pellandor",
    "Ravenmere",
    "Stoneriver",
    "Thistlecote",
    "Wrenfield",
    "Zephyrine",
    "Calderwyn",
    "Ellisport",
    "Gravenholt",
    "Ironbrook",
    "Juniperon",
    "Larkspire",
    "Mirelock",
    "Nettlebay",
    "Oakhurst",
    "Pinecastle",
    "Rosmerta",
    "Sablecourt",
    "Tamberlin",
    "Umberfield",
    "Valecrest",
    "Westerlyn",
    "Yarrowden",
    "Ashcombe",
    "Bramblecot",
    "Cindermoor",
)
SECOND_WORDS = (
    "Dynamics",
    "Works",
    "Systems",
    "Holdings",
    "Partners",
    "Labs",
    "Logistics",
    "Ventures",
    "Industries",
    "Solutions",
    "Networks",
    "Analytics",
    "Trading",
    "Foundry",
    "Media",
    "Energy",
    "Capital",
    "Freight",
    "Health",
    "Robotics",
)
FIRST_NAMES = (
    "Alden",
    "Brielle",
    "Corwin",
    "Delphine",
    "Emrys",
    "Fenella",
    "Garrick",
    "Hollis",
    "Isolde",
    "Jareth",
    "Kesia",
    "Lorcan",
    "Maelis",
    "Niamh",
    "Orrin",
    "Perrin",
)
LAST_NAMES = (
    "Ashdown",
    "Bellweather",
    "Carrow",
    "Dunmore",
    "Ellery",
    "Farrowby",
    "Gilmartin",
    "Harrowgate",
    "Ingleby",
    "Kettering",
    "Lowther",
    "Marchbank",
    "Northcote",
    "Penhallow",
    "Quenby",
    "Rothwell",
    "Selwood",
    "Trevanion",
    "Upworth",
    "Wycombe",
)

SUFFIXES = (
    "pty ltd",
    "co. kg",
    "s.a.",
    "s.r.l.",
    "b.v.",
    "n.v.",
    "inc.",
    "inc",
    "ltd.",
    "ltd",
    "llc",
    "llp",
    "plc",
    "gmbh",
    "corp.",
    "corp",
    "corporation",
    "company",
    "co.",
    "co",
    "ag",
    "sa",
    "srl",
    "bv",
    "nv",
    "oy",
    "ab",
    "as",
    "kg",
    "limited",
    "group",
)
_SUFFIX_RE = re.compile(
    r"^(?P<body>.*?)(?P<sep>[\s,]+)(?P<suffix>"
    + "|".join(re.escape(s) for s in sorted(SUFFIXES, key=len, reverse=True))
    + r")$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NameShape:
    """What is kept from a real name: words to replace, separator + suffix, casing style."""

    words: int
    tail: str
    style: str


def _style(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "title"
    if all(c.isupper() for c in letters) and len(letters) > 1:
        return "upper"
    if all(c.islower() for c in letters):
        return "lower"
    return "title"


def shape_of(original: str) -> NameShape:
    """Split a real name into body word count, the verbatim suffix tail and the casing style."""
    m = _SUFFIX_RE.match(original)
    body, tail = (m.group("body"), m.group("sep") + m.group("suffix")) if m else (original, "")
    if not body.strip():
        body, tail = original, ""
    words = max(1, min(4, len(body.split())))
    return NameShape(words=words, tail=tail, style=_style(body))


def apply_style(text: str, style: str) -> str:
    """Apply one casing style (upper/lower/title) to a generated name."""
    if style == "upper":
        return text.upper()
    if style == "lower":
        return text.lower()
    return text


def keyed_index(salt: bytes, key: str, attempt: int, size: int) -> int:
    """Stable index into a pool of ``size`` for (salt, key, attempt)."""
    digest = hmac.new(salt, f"{attempt}\x00{key}".encode(), hashlib.sha256).digest()
    return int.from_bytes(digest[:8], "big") % size


def _body(salt: bytes, key: str, attempt: int, words: int) -> str:
    parts = [ROOTS[keyed_index(salt, key, attempt, len(ROOTS))]]
    if words >= 2:
        parts.append(SECOND_WORDS[keyed_index(salt, key + "#2", attempt, len(SECOND_WORDS))])
    for extra in range(3, words + 1):
        pool = FIRST_NAMES if extra % 2 else SECOND_WORDS
        parts.insert(0, pool[keyed_index(salt, f"{key}#{extra}", attempt, len(pool))])
    return " ".join(parts)


def _disambiguated(salt: bytes, key: str, words: int) -> str:
    """Last resort after MAX_DUMMY_ATTEMPTS: a deterministic invented root."""
    digest = hmac.new(salt, ("x\x00" + key).encode(), hashlib.sha256).hexdigest()
    letters = "".join(chr(ord("a") + int(c, 16) % 26) for c in digest[:6])
    root = "Vel" + letters
    body = _body(salt, key, 0, words).split(" ")
    body[-1 if words == 1 else 0] = root
    return " ".join(body)


def company_body(salt: bytes, words: int, key: str, is_free: Callable[[str], bool]) -> str:
    """A fictional Title-case company body of ``words`` words for which ``is_free`` holds.

    No legal suffix and no casing is applied: the caller appends the original's verbatim suffix
    and restyles the body to the original's case, so one canonical body serves every case variant.
    """
    for attempt in range(MAX_DUMMY_ATTEMPTS):
        candidate = _body(salt, key, attempt, words)
        if candidate.casefold() not in GENERIC_WORDS and is_free(candidate):
            return candidate
    for n in range(MAX_DUMMY_ATTEMPTS):
        candidate = _disambiguated(salt, f"{key}#{n}", words)
        if is_free(candidate):
            return candidate
    raise MaskingError("no free stand-in name could be generated")


def slug_of(body: str) -> str:
    """Lower-case ASCII slug of a dummy company body (its first word) for a fake domain."""
    words = body.split()
    root = words[0] if words else body
    return re.sub(r"[^a-z0-9]", "", root.lower()) or "lumenfold"


def person_word(salt: bytes, key: str, first: bool, is_free: Callable[[str], bool]) -> str:
    """A fictional first name (``first``) or surname for one word of a contact's name."""
    pool = FIRST_NAMES if first else LAST_NAMES
    for attempt in range(MAX_DUMMY_ATTEMPTS):
        candidate = pool[keyed_index(salt, key, attempt, len(pool))]
        if is_free(candidate):
            return candidate
    for n in range(MAX_DUMMY_ATTEMPTS):
        candidate = _disambiguated(salt, f"{key}#{n}", 1)
        if is_free(candidate):
            return candidate
    raise MaskingError("no free stand-in name could be generated")
