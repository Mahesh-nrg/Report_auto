"""The masking boundary of the importer's LLM fallback (docs/IMPORTER_DESIGN.md §i).

* ``call_header`` is the only call that sends real client names: it reads company, dates and
  scope from the cover/header excerpt, and reading those names is its whole job. Its input is the
  reader's ``header_text`` only, never finding content.
* ``call_masked_items`` handles every call that carries finding content. Each finding item is
  masked value by value BEFORE it is serialised to JSON (masking the serialised text would miss
  a name glued to an escaped ``\n`` or ``\t``): client terms, the scope host names, IPs,
  e-mails and URL hosts become believable stand-ins, while paths, query names and payloads are
  sent as they are. The answer is unmasked, and any stand-in or legacy placeholder left in it
  rejects the whole answer. ``call_masked`` does the same for one plain-text message.
  When masking is on and the header names no client at all, the fallback makes no finding call
  (fail closed, ``has_client_name``): there would be nothing to mask the client's name with.

The masker is created per job and never persisted or logged; nothing here logs text.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

from ..masking import Masker, NullMasker
from ..stand_ins import prepare_corpus, stray_stand_ins
from .errors import FallbackRejected
from .model import HeaderFields


def scope_hosts(scope: Sequence[str]) -> tuple[str, ...]:
    """Host names of the scope entries (``www.`` dropped) and their last two labels, so a bare
    ``api.client.test`` in finding text is masked too (the masking layer masks URLs, IPs and
    e-mails by shape, but not bare host names). IP addresses are left to the masking layer."""
    hosts: list[str] = []
    for item in scope:
        text = item.strip()
        try:
            host = urlsplit(text if "://" in text else f"//{text}").hostname or ""
        except ValueError:
            continue
        host = host.removeprefix("www.").strip(".")
        if "." not in host or not any(c.isalpha() for c in host) or ":" in host:
            continue
        hosts += [host, ".".join(host.split(".")[-2:])]
    return tuple(dict.fromkeys(hosts))


def has_client_name(header: HeaderFields) -> bool:
    """True when the header names the client, so finding text can be masked for it."""
    return any(name.strip() for name in (header.company, *header.company_candidates))


def _terms(header: HeaderFields, extra_terms: Sequence[str]) -> list[tuple[str, str]]:
    names: list[str] = [header.company, *header.company_candidates, *header.leftover_names]
    names += scope_hosts(header.scope)
    for contact in header.contacts:
        full = f"{contact.firstname} {contact.lastname}"
        names += [contact.email, full, contact.firstname, contact.lastname]
    terms = [("CLIENT", name) for name in names]
    terms += [("TERM", term) for term in extra_terms]
    return [(kind, " ".join(term.split())) for kind, term in terms if term and term.strip()]


def finding_masker(
    *, enabled: bool, header: HeaderFields, extra_terms: Sequence[str]
) -> Masker | NullMasker:
    """The masker for finding calls; a NullMasker only when masking is off and nothing is named
    (the same rule as ``docx_jobs.masker_factory_for``)."""
    terms = _terms(header, extra_terms)
    if not enabled and not terms:
        return NullMasker()
    return Masker(terms)


def _answer(llm: Any, system: str, user: str, tool: dict) -> dict:
    reply = llm.call_tool(system, user, tool)
    args = reply[0] if isinstance(reply, tuple) else reply
    if not isinstance(args, dict):
        raise FallbackRejected()
    return args


KEPT_ITEM_KEYS = frozenset({"key", "need"})  # importer bookkeeping, never client text


def _has_placeholder_text(value: Any, masker: Masker | NullMasker, sent: Any = None) -> bool:
    """True when ``value`` still holds a stand-in (or legacy placeholder) not in ``sent``."""
    try:
        return bool(stray_stand_ins(masker, value, sent))
    except ValueError:  # nested too deep to scan: never treat it as clean
        return True


def _restored(masker: Masker | NullMasker, args: dict, sent: Any) -> dict:
    try:
        restored = masker.unmask(args)
    except ValueError:
        raise FallbackRejected() from None
    if not isinstance(restored, dict) or _has_placeholder_text(restored, masker, sent):
        raise FallbackRejected()
    return restored


def prepare(masker: Masker | NullMasker, corpus: Any) -> None:
    """Register the report's finding text before the first batch (no stand-in may equal it)."""
    prepare_corpus(masker, corpus)


def mask_items(masker: Masker | NullMasker, items: Sequence[dict]) -> list[dict]:
    """Each item with every text value masked; ``key`` and ``need`` are kept as they are."""
    return [
        {k: v if k in KEPT_ITEM_KEYS else masker.mask_json_plain(v) for k, v in item.items()}
        for item in items
    ]


def call_masked(llm: Any, masker: Masker | NullMasker, system: str, user: str, tool: dict) -> dict:
    """Mask ``user``, call the tool, unmask the answer and reject leftover stand-ins.

    ``LlmError`` from the client propagates; a malformed answer raises ``FallbackRejected``.
    """
    args = _answer(llm, system, masker.mask_plain(user), tool)
    return _restored(masker, args, user)


def call_masked_items(
    llm: Any,
    masker: Masker | NullMasker,
    system: str,
    items: Sequence[dict],
    render: Callable[[Sequence[dict]], str],
    tool: dict,
) -> dict:
    """Mask each item, serialise the masked items with ``render``, call the tool and return the
    restored answer (stand-ins left in it raise ``FallbackRejected``, as in ``call_masked``)."""
    try:
        masked = mask_items(masker, items)
    except ValueError:  # too deep or a stand-in collision: nothing is sent
        raise FallbackRejected() from None
    args = _answer(llm, system, render(masked), tool)
    return _restored(masker, args, list(items))


def call_header(llm: Any, system: str, user: str, tool: dict) -> dict:
    """The unmasked header/metadata call: ``user`` holds only the cover/header excerpt."""
    return _answer(llm, system, user, tool)


__all__ = [
    "call_header",
    "call_masked",
    "call_masked_items",
    "finding_masker",
    "has_client_name",
    "mask_items",
    "prepare",
    "scope_hosts",
]
