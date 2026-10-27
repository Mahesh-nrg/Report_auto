"""Vulnerability library: save audit findings as reusable PwnDoc entries and reuse their wording.

Two jobs:
* ``save_findings`` turns finished findings into library entries (``POST /api/vulnerabilities``)
  with everything audit-specific removed - screenshots, proof of concept, scope, the observation
  (what was seen at THIS client) and client specific custom fields (URL, host, ...) - skips titles
  the library already has in any language, and refuses a finding whose wording still names the
  client, a host, an IP or an e-mail (the library is readable by every PwnDoc user and its text is
  reused in other clients' prompts);
* ``reference_for`` finds the closest approved entry for a title and renders its wording as short
  plain text, so a later refinement can stay consistent with what the team already approved.

Library content is shared and editable by others, so ``reference_for`` returns plain text only.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from .cvss import parse_vector
from .sanitize import sanitize_html

log = logging.getLogger(__name__)

LOCALE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$")
IMG_TAG = re.compile(r"""<img\b(?:[^>"']|"[^"]*"|'[^']*')*>""", re.IGNORECASE)
EMPTY_PARAGRAPH = re.compile(r"<p>\s*(?:<br\s*/?>\s*)*</p>", re.IGNORECASE)
BLOCK_END = re.compile(r"</(?:p|li|h[1-6]|pre|blockquote|tr|ul|ol)>|<br\s*/?>", re.IGNORECASE)
TAG = re.compile(r"<[^>]+>")
# Client / engagement specific custom fields. "ip" only counts as a whole word so that labels
# such as "Description" are not caught.
CLIENT_SPECIFIC_LABEL = re.compile(r"url|host|location|parameter|\bip\b", re.IGNORECASE)
PRIORITIES = frozenset({1, 2, 3, 4})
COMPLEXITIES = frozenset({1, 2, 3})
MAX_REFERENCES = 40
MAX_REFERENCE_LEN = 500
MAX_REFERENCE_LINES = 10
DEFAULT_THRESHOLD = 0.85
DEFAULT_MAX_CHARS = 2500
ELLIPSIS = "…"
TEXT_FIELDS = ("description", "remediation")  # never "observation": it is client specific
DUPLICATE_TITLE = re.compile(r'details\.title:\s*"((?:[^"\\]|\\.)*)"')


@dataclass(frozen=True)
class SaveResult:
    created: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]  # (title, reason)


# -- text helpers -------------------------------------------------------------------------------
def plain_text(value: Any) -> str:
    """Visible text of an HTML fragment: scripts dropped, tags removed, paragraphs on lines."""
    if not isinstance(value, str) or not value.strip():
        return ""
    cleaned = BLOCK_END.sub("\n", sanitize_html(value))
    text = html.unescape(TAG.sub("", cleaned))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def normalise_title(title: Any) -> str:
    return " ".join(title.casefold().split()) if isinstance(title, str) else ""


def _strip_images(value: Any) -> str:
    """Remove ``<img>`` tags (and the paragraphs they leave empty); keep all other markup."""
    if not isinstance(value, str):
        return ""
    stripped, removed = IMG_TAG.subn("", value)
    return EMPTY_PARAGRAPH.sub("", stripped) if removed else stripped


def _check_locale(locale: Any) -> str:
    if not isinstance(locale, str) or not LOCALE.match(locale):
        raise ValueError(f"invalid locale: {locale!r}")
    return locale


# -- finding -> library entry -------------------------------------------------------------------
def _references(value: Any) -> list[str]:
    items = value.splitlines() if isinstance(value, str) else value
    if not isinstance(items, list | tuple):
        return []
    cleaned = [i.strip()[:MAX_REFERENCE_LEN] for i in items if isinstance(i, str) and i.strip()]
    return cleaned[:MAX_REFERENCES]


def _custom_field_id_and_label(entry: Mapping) -> tuple[str, str]:
    ref = entry.get("customField")
    if isinstance(ref, str):
        return ref.strip(), ""
    if isinstance(ref, Mapping):
        cid, label = ref.get("_id"), ref.get("label")
        return (cid.strip() if isinstance(cid, str) else ""), (
            label if isinstance(label, str) else ""
        )
    return "", ""


def _custom_text(value: Any) -> Any:
    """Text a library entry may keep, or ``None`` when the shape is not one PwnDoc stores."""
    if isinstance(value, str):
        return _strip_images(value)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return [_strip_images(v) for v in value]
    if isinstance(value, int | float | bool):
        return value
    return None


def _library_custom_fields(finding: Mapping) -> list[dict[str, Any]]:
    entries = finding.get("customFields")
    kept: list[dict[str, Any]] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, Mapping):
            continue
        cid, label = _custom_field_id_and_label(entry)
        text = _custom_text(entry.get("text"))
        if not cid or text is None or CLIENT_SPECIFIC_LABEL.search(label):
            continue
        kept.append({"customField": cid, "text": text})
    return kept


def _choice(value: Any, allowed: frozenset[int]) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value in allowed
        else None
    )


def _vector(value: Any) -> str | None:
    return value.strip() if parse_vector(value) is not None else None


def finding_to_vulnerability(finding: Mapping, locale: str) -> dict[str, Any]:
    """Body item for ``POST /api/vulnerabilities`` from an audit finding (input is not modified)."""
    _check_locale(locale)
    title = finding.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("finding has no title")
    detail = {
        "locale": locale,
        "title": title.strip(),
        "vulnType": str(finding.get("vulnType") or "").strip(),
        **{name: _strip_images(finding.get(name)) for name in TEXT_FIELDS},
        "references": _references(finding.get("references")),
        "customFields": _library_custom_fields(finding),
    }
    item: dict[str, Any] = {
        "cvssv3": _vector(finding.get("cvssv3")),
        "priority": _choice(finding.get("priority"), PRIORITIES),
        "remediationComplexity": _choice(finding.get("remediationComplexity"), COMPLEXITIES),
        "details": [detail],
    }
    category = finding.get("category")
    if isinstance(category, str) and category.strip():
        item["category"] = category.strip()
    return item


# -- library lookups ----------------------------------------------------------------------------
def _details_of(item: Any, locale: str | None) -> Iterator[Mapping]:
    """Detail records of one library item in ``locale`` (any language when ``None``).

    Both PwnDoc list shapes are understood; malformed entries are skipped.
    """
    if not isinstance(item, Mapping):
        return
    candidates = item.get("details")
    pool = list(candidates) if isinstance(candidates, list) else []
    if isinstance(item.get("detail"), Mapping):  # GET /api/vulnerabilities/:locale shape
        pool.append(item["detail"])
    for detail in pool:
        if isinstance(detail, Mapping) and (locale is None or detail.get("locale") == locale):
            if isinstance(detail.get("title"), str) and detail["title"].strip():
                yield detail


def _existing_titles(library: Any) -> set[str]:
    """Titles in every language: PwnDoc keeps ``details.title`` unique across locales."""
    items = library if isinstance(library, list) else []
    return {
        normalise_title(detail["title"]) for item in items for detail in _details_of(item, None)
    }


# -- client data check --------------------------------------------------------------------------
UNCHECKED_REASON = "cannot check for client data (no sensitive-data masker); not saved"


def _strings_of(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple):
        return [v for v in value if isinstance(v, str)]
    return []


def _saved_strings(item: Mapping) -> list[str]:
    """Every string of the library entry that would be saved (raw HTML, so href values count)."""
    detail = item["details"][0]
    texts = [detail["title"], detail["vulnType"], str(item.get("category") or "")]
    texts += [detail[name] for name in TEXT_FIELDS]
    texts += detail["references"]
    for entry in detail["customFields"]:
        texts += _strings_of(entry["text"])
    return [t for t in texts if t]


def _finding_texts(item: Mapping) -> list[str]:
    """What a viewer of the saved entry could see or follow: the raw markup and its plain text."""
    raw = _saved_strings(item)
    return [*raw, *(plain_text(t) for t in raw if "<" in t)]


def _classify(sensitive: Any, texts: Sequence[str]) -> list[str] | None:
    """Kinds of client data found, or ``None`` when the masker failed to classify."""
    kinds: list[str] = []
    for text in texts:
        try:
            kinds += sensitive(text)
        except Exception:  # noqa: BLE001 - a broken masker must fail closed, not open
            log.exception("client data check failed")
            return None
    return kinds


def _client_data_reason(item: Mapping, masker: Any) -> str | None:
    """Why this entry must not go into a shared library, or ``None`` when it looks generic.

    Fails closed: with no masker, or one that cannot classify text, nothing is saved.
    """
    sensitive = getattr(masker, "sensitive_kinds", None)
    if not callable(sensitive):
        return UNCHECKED_REASON
    kinds = _classify(sensitive, _finding_texts(item))
    if kinds is None:
        return UNCHECKED_REASON
    if not kinds:
        return None
    names = ", ".join(dict.fromkeys(kinds))
    return f"mentions client data ({names}); remove it from the finding first"


# -- saving -------------------------------------------------------------------------------------
def _refused_titles(response: Any) -> set[str]:
    """Normalised titles PwnDoc named in its ``duplicates`` messages."""
    duplicates = response.get("duplicates") if isinstance(response, Mapping) else None
    return {
        normalise_title(m.group(1))
        for message in (duplicates if isinstance(duplicates, list) else [])
        for m in DUPLICATE_TITLE.finditer(str(message))
    }


def _reconcile(
    titles: Sequence[str], response: Any
) -> tuple[tuple[str, ...], list[tuple[str, str]]]:
    """``(titles that really exist now, titles PwnDoc refused with a reason)``; pure."""
    if not isinstance(response, Mapping) or not response.get("duplicates"):
        return tuple(titles), []
    refused = _refused_titles(response)
    kept = [t for t in titles if normalise_title(t) not in refused]
    skipped = [(t, "already in the library") for t in titles if normalise_title(t) in refused]
    created = response.get("created")
    if isinstance(created, int) and not isinstance(created, bool) and created < len(kept):
        unknown = len(kept) - max(created, 0)
        skipped += [(t, "PwnDoc reported a duplicate; check the library") for t in kept[-unknown:]]
        kept = kept[:-unknown]
    return tuple(kept), skipped


def _title(item: Mapping) -> str:
    return item["details"][0]["title"]


def _colliding(exc: Exception, items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The items a PwnDoc 'title already exists' 400 is about (none for any other error)."""
    if getattr(exc, "status", None) != 400 or "already exists" not in str(exc).lower():
        return []
    named = {
        normalise_title(m.group(1).replace('\\"', '"')) for m in DUPLICATE_TITLE.finditer(str(exc))
    }
    hit = [i for i in items if normalise_title(_title(i)) in named]
    return hit if hit or len(items) != 1 else list(items)  # a lone item can only be the culprit


def _create(
    client: Any, items: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], Any, list[tuple[str, str]]]:
    """POST the items. Returns ``(items PwnDoc took, its response, titles it refused)``.

    A 400 naming the colliding title(s) drops just those and retries the rest once; a 400 that
    names no title of this batch (several items) is raised rather than blamed on everyone.
    """
    refused: list[tuple[str, str]] = []
    pending = list(items)
    for attempt in (1, 2):
        try:
            return pending, client.create_vulnerabilities(pending), refused
        except Exception as exc:  # noqa: BLE001 - only this one PwnDoc answer is expected
            hit = _colliding(exc, pending)
            if attempt == 2 or not hit:
                raise
            refused += [(_title(i), "already in the library") for i in hit]
            pending = [i for i in pending if all(i is not h for h in hit)]
            if not pending:
                return pending, None, refused
    raise AssertionError("unreachable")  # pragma: no cover


def _plan(
    findings: Sequence[Mapping], known: set[str], locale: str, masker: Any
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    items: list[dict[str, Any]] = []
    batch_keys: set[str] = set()
    skipped: list[tuple[str, str]] = []
    for finding in findings:
        title = finding.get("title") if isinstance(finding, Mapping) else None
        key = normalise_title(title)
        if not key:
            skipped.append((str(title or ""), "missing title"))
        elif key in known:
            reason = "duplicate in this batch" if key in batch_keys else "already in the library"
            skipped.append((title.strip(), reason))
        else:
            item = finding_to_vulnerability(finding, locale)
            if (reason := _client_data_reason(item, masker)) is not None:
                skipped.append((title.strip(), reason))
                continue
            known.add(key)
            batch_keys.add(key)
            items.append(item)
    return items, skipped


def save_findings(
    client: Any, findings: Sequence[Mapping], locale: str, masker: Any = None
) -> SaveResult:
    """Add findings to the library, skipping duplicates and anything naming the client.

    ``masker`` is the audit's :class:`~pwndoc_ai.masking.Masker`: an entry whose saved text (raw
    markup included: link targets), vulnerability type, category, references or kept custom
    fields hold a client term, IP, e-mail or client URL is skipped with a reason instead of being
    copied to the shared library. Without a masker that can classify text nothing is saved.
    """
    _check_locale(locale)
    known = _existing_titles(client.list_vulnerabilities())
    items, skipped = _plan(findings, known, locale, masker)
    created: tuple[str, ...] = ()
    if items:
        taken, response, refused = _create(client, items)
        skipped += refused
        if taken:
            created, unknown = _reconcile([_title(i) for i in taken], response)
            skipped += unknown
    return SaveResult(created, tuple(skipped))


# -- reuse of approved wording ------------------------------------------------------------------
def _closest(
    title: str, library: Sequence[Mapping], locale: str, threshold: float
) -> Mapping[str, Any] | None:
    wanted = normalise_title(title)
    best, best_ratio = None, 0.0
    for item in library if isinstance(library, Sequence) else []:
        for detail in _details_of(item, locale):
            ratio = SequenceMatcher(None, wanted, normalise_title(detail["title"])).ratio()
            if ratio >= threshold and ratio > best_ratio:
                best, best_ratio = detail, ratio
    return best


def _render(detail: Mapping) -> str:
    sections = [
        ("Description", plain_text(detail.get("description"))),
        ("Remediation", plain_text(detail.get("remediation"))),
    ]
    refs = [plain_text(r) for r in _references(detail.get("references"))[:MAX_REFERENCE_LINES]]
    body = [f"{name}: {text}" for name, text in sections if text]
    if any(refs):
        body.append("References:\n" + "\n".join(f"- {r}" for r in refs if r))
    if not body:
        return ""  # a bare title (list endpoint) carries no wording worth reusing
    return "\n".join([f"Title: {plain_text(detail['title'])}", *body])


def reference_for(
    title: str,
    library_items: Sequence[Mapping],
    locale: str,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Approved wording of the closest library entry as plain text, ``""`` when nothing fits."""
    if max_chars <= 0 or not normalise_title(title) or not isinstance(locale, str):
        return ""
    detail = _closest(title, library_items, locale, threshold)
    text = _render(detail) if detail is not None else ""
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + ELLIPSIS
