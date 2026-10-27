"""Validation of one paragraph proposed by the model against the original text."""

from __future__ import annotations

from dataclasses import dataclass

from . import limits
from .facts import TRUSTED_HOSTS, Facts, extract_facts, reference_numbers, url_host
from .textclean import clean_model_text

LIGHT, DEEP = "light", "deep"
SMALL_ALLOWANCE = 12  # characters a very short paragraph may grow by whatever the ratio says


@dataclass(frozen=True)
class Verdict:
    status: str  # "applied", "rejected" or "unchanged"
    reason: str
    text: str  # the cleaned proposed text ("" for a non-text answer)


def _reject(reason: str, text: str = "") -> Verdict:
    return Verdict("rejected", reason, text)


def _length_reason(old: str, new: str, mode: str) -> str:
    low, high = limits.LIGHT_RATIO if mode == LIGHT else limits.DEEP_RATIO
    if len(new) < len(old) * low:
        return "the rewrite is much shorter than the original"
    if len(new) > max(len(old) * high, len(old) + SMALL_ALLOWANCE):
        return "the rewrite is much longer than the original"
    return ""


def _kept_reason(old: Facts, new: Facts) -> str:
    """Everything that identifies evidence in the original must still be in the new text."""
    checks = (
        (old.addresses - new.addresses, "an IP address was changed or removed"),
        (old.emails - new.emails, "an e-mail address was changed or removed"),
        (old.urls - new.urls, "a link was changed or removed"),
        (old.ids - new.ids, "a CVE or CWE id was changed or removed"),
        (old.code - new.code, "code or a technical token was changed or removed"),
    )
    return next((message for missing, message in checks if missing), "")


def _light_reason(old: Facts, new: Facts) -> str:
    """A copy edit adds no fact: no new number, address, link or id."""
    if old.numbers - new.numbers or new.numbers - old.numbers:
        return "a number was changed"
    added = (
        new.addresses - old.addresses,
        new.emails - old.emails,
        new.urls - old.urls,
        new.ids - old.ids,
    )
    return "a fact was added" if any(added) else ""


def _invented_reason(old: Facts, new: Facts) -> str:
    """Deep mode may add references, never evidence: hosts, addresses, mailboxes, odd links.

    A link is accepted only on a trusted reference site. The research text is untrusted web
    content, so a link being in it proves nothing (a page could plant one for the model to copy).
    """
    if new.addresses - old.addresses or new.emails - old.emails:
        return "an address that is not in the document was added"
    for url in new.urls - old.urls:
        if url_host(url) not in TRUSTED_HOSTS:
            return "a link that is not in the document or on a trusted reference site was added"
    return ""


def _deep_numbers_reason(old: Facts, new: Facts, text: str, research: str) -> str:
    """Ports, versions, counts and scores are evidence: deep mode keeps every number of the
    original and adds one only when the research states it or it names a public reference."""
    if old.numbers - new.numbers:
        return "a number of the original was changed or removed"
    allowed = old.numbers | extract_facts(research).numbers | reference_numbers(text)
    if new.numbers - allowed:
        return "a number that is not in the document or the research was added"
    return ""


def _squash(text: str) -> str:
    """The text with every run of whitespace reduced to one space and the edges trimmed."""
    return " ".join(text.split())


def _with_original_padding(original: str, proposed: str) -> str:
    """Models strip the spaces around a paragraph; carry the original's over so the edges of
    the paragraph are never shown as a deletion or an insertion."""
    body = proposed.strip()
    lead = original[: len(original) - len(original.lstrip())]
    trail = original[len(original.rstrip()) :]
    return lead + body + trail


def validate_paragraph(
    original: str, proposed: object, *, mode: str, research: str = ""
) -> Verdict:
    """Judge one paragraph returned by the model. ``applied`` carries the text to write."""
    if not isinstance(proposed, str):
        return _reject("the answer was not text")
    new = clean_model_text(proposed, original)
    if not new.strip():
        return _reject("the answer was empty", new)
    new = _with_original_padding(original, new)
    if _squash(new) == _squash(original):
        return Verdict("unchanged", "", original)  # whitespace alone is never worth a tracked edit
    if len(new) > limits.MAX_OUTPUT_PARAGRAPH_CHARS:
        return _reject("the answer was too long", "")
    old_facts, new_facts = extract_facts(original), extract_facts(new)
    reason = (
        _length_reason(original, new, mode)
        or _kept_reason(old_facts, new_facts)
        or (_light_reason(old_facts, new_facts) if mode == LIGHT else "")
        or (_invented_reason(old_facts, new_facts) if mode == DEEP else "")
        or (_deep_numbers_reason(old_facts, new_facts, new, research) if mode == DEEP else "")
        or ("markup was added" if new_facts.tags - old_facts.tags else "")
    )
    return _reject(reason, new) if reason else Verdict("applied", "", new)


def validate_addition(text: object, *, source: str, research: str = "") -> Verdict:
    """Judge a paragraph the model wants to add (deep mode). ``source`` is the text it follows."""
    if not isinstance(text, str):
        return _reject("the answer was not text")
    new = clean_model_text(text, source)
    if not new.strip():
        return _reject("the added paragraph was empty")
    if len(new) > limits.MAX_ADDITION_CHARS:
        return _reject("the added paragraph was too long")
    facts, source_facts = extract_facts(new), extract_facts(source)
    reason = _invented_reason(source_facts, facts)
    if not reason and facts.numbers - source_facts.numbers:
        allowed = extract_facts(research).numbers | reference_numbers(new)
        if (facts.numbers - source_facts.numbers) - allowed:
            reason = "a number that is not in the document or the research was added"
    reason = reason or ("markup was added" if facts.tags else "")
    return _reject(reason, new) if reason else Verdict("applied", "", new)
