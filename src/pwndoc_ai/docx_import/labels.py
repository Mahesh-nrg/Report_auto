"""Text normalisation and the label dictionary of the deterministic reader (no lxml).

``norm`` is for matching (labels, names, titles); ``clean_line`` keeps the document's own
characters for values shown to the user. Labels are compared by ``label_key``: the normalised
text with everything but letters and digits removed, so ``"Vulnerable Path/ Port/ URL"`` and
``"vulnerable path port url"`` are the same label.
"""

from __future__ import annotations

import re
import unicodedata

from . import profile

TEMPLATE_LEFTOVER_NAMES = frozenset({"alfred ai"})

BLANK_RE = re.compile(
    r"(?:|n\s*/?\s*a|-+|\u2013+|\u2014+|_+|\.+|null|none|nil|undefined|not applicable|tbd)",
    re.IGNORECASE,
)
_DROP = dict.fromkeys(map(ord, "\u00ad\u200b\u200c\u200d\u2060\ufeff"), None)
_MAP = str.maketrans(
    {
        "\u00a0": " ", "\u2007": " ", "\u202f": " ", "\u2018": "'", "\u2019": "'",
        "\u201a": "'", "\u201b": "'", "\u201c": '"', "\u201d": '"', "\u201e": '"',
        "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
        "\u2015": "-", "\u2212": "-",
    }
)  # fmt: skip
_SPACE = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_LINE_SPACE = re.compile(r"[ \t\f\v\u00a0\u2007\u202f\u3000]+")
_NON_ALNUM = re.compile(r"[^a-z0-9]")
_NAME_SPLIT = re.compile(r"\s{3,}|\t")

# Row labels of the detail table (and synonyms seen in other templates), by label_key.
FIELD_LABELS: dict[str, str] = {
    **dict.fromkeys(
        ("nameofvulnerability", "nameofthevulnerability", "vulnerabilityname", "vulnerability",
         "vulnerabilitytitle", "title", "findingtitle", "finding", "issue", "issuetitle"),
        "title",
    ),
    **dict.fromkeys(
        ("vulnerablelocation", "location", "affectedlocation", "affectedcomponent"), "location"
    ),
    **dict.fromkeys(
        ("vulnerablepathporturl", "vulnerablepathurl", "vulnerableurl", "vulnerableurls", "url",
         "urls", "affectedurl", "affectedurls", "endpoint", "endpoints", "path", "vulnerablepath"),
        "urls",
    ),
    **dict.fromkeys(
        ("vulnerableparameter", "vulnerableparameters", "parameter", "parameters",
         "affectedparameter"),
        "parameter",
    ),
    **dict.fromkeys(
        ("cvecwe", "cwecve", "cwe", "cve", "cweid", "cveid", "referencescveidcweid"), "cwe"
    ),
    **dict.fromkeys(
        ("cvssscore", "cvss", "cvssv3", "cvss31", "cvss30", "cvssv31", "cvssbasescore",
         "cvssvector", "basescore", "score"),
        "cvss",
    ),
    **dict.fromkeys(("severity", "risk", "riskrating", "risklevel", "rating"), "severity"),
    **dict.fromkeys(
        ("description", "details", "vulnerabilitydescription", "summary"), "description"
    ),
    **dict.fromkeys(("observation", "observations"), "observation"),
    **dict.fromkeys(("proofofconcept", "poc", "stepstoreproduce", "evidence"), "poc"),
    **dict.fromkeys(
        ("recommendations", "recommendation", "remediation", "mitigation", "solution", "fix"),
        "remediation",
    ),
    **dict.fromkeys(("references", "reference", "links"), "references"),
    **dict.fromkeys(("vulnerablepointerrorlocation", "vulnerablepoint"), "group"),
    **dict.fromkeys(("sno", "no", "serialno", "srno", "id", "findingid"), "id"),
}  # fmt: skip
POC_INLINE_LABELS = frozenset({"proofofconcept", "poc"})
SEVERITY_WORDS: dict[str, str] = {
    "critical": "Critical",
    "high": "High",
    "medium": "Medium",
    "moderate": "Medium",
    "low": "Low",
    "informational": "Informational",
    "information": "Informational",
    "info": "Informational",
}
CONTACT_LABELS = frozenset(
    {"contact", "contactperson", "clientcontact", "pointofcontact", "spoc", "email", "contactemail"}
)


def norm(text: str) -> str:
    """NFKC, nbsp to space, soft hyphen and zero-width characters removed, smart quotes and
    dashes mapped to ASCII, whitespace collapsed and stripped."""
    value = unicodedata.normalize("NFKC", str(text).translate(_DROP)).translate(_MAP)
    return _SPACE.sub(" ", value).strip()


def fold_title(text: str) -> str:
    """Lower-case letters and digits only: ``"XML-RPC"`` and ``"XMLRPC"`` fold the same."""
    return _NON_ALNUM.sub("", norm(text).lower())


def label_key(text: str) -> str:
    return _NON_ALNUM.sub("", norm(text).casefold())


def name_key(text: str) -> str:
    """Case-insensitive key for company and audit names."""
    return norm(text).casefold()


def is_blank(text: str) -> bool:
    """True for empty text and the placeholders the generator writes (``undefined``, ``N/A``,
    ``-``, ``null``...)."""
    return BLANK_RE.fullmatch(norm(text).strip(" :;,")) is not None


def field_of(label: str) -> str:
    """The finding field a row label names, or ``""`` (the active layout profile's labels
    first)."""
    key = label_key(label)
    return profile.active().field_labels.get(key) or FIELD_LABELS.get(key, "")


def severity_of_text(text: str) -> str:
    """``"High"`` for ``"high"``, ``" HIGH "`` or ``"High risk"``; ``""`` when unknown."""
    words = norm(text).casefold().split()
    if not words:
        return ""
    return SEVERITY_WORDS.get(words[0].strip(".:;,()"), "")


def clean_line(text: str) -> str:
    """One display line: invisible characters dropped, every whitespace run (newlines and tabs
    included) collapsed to one space, control characters removed. Quotes are kept as written."""
    value = unicodedata.normalize("NFC", str(text).translate(_DROP))
    value = _CONTROL.sub(" ", value.replace("\n", " ").replace("\r", " ").replace("\t", " "))
    return _SPACE.sub(" ", value).strip()


def clean_inline(text: str) -> str:
    """Like ``clean_line`` but for one line of rich text (newlines are not expected here)."""
    value = unicodedata.normalize("NFC", str(text).translate(_DROP))
    return _LINE_SPACE.sub(" ", _CONTROL.sub(" ", value)).strip()


def first_name_segment(text: str) -> str:
    """The text before the first wide gap (three spaces or a tab): the company in a header line
    such as ``"Brandarmy            ParadigmIT Cybersecurity"``."""
    for part in _NAME_SPLIT.split(str(text)):
        cleaned = clean_line(part)
        if cleaned:
            return cleaned
    return ""


def cap(text: str, limit: int) -> tuple[str, bool]:
    """``text`` cut to ``limit`` characters, and whether it was cut."""
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip(), True
