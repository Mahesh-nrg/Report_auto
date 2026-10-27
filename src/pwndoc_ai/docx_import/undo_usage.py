"""What the rest of PwnDoc still uses, read before an undo deletes a shared object.

An undo may delete a journaled company, contact or screenshot only when nothing outside the
import still points at it. PwnDoc lists only the caller's own audits to a non-administrator, so
for anyone but an administrator the answer is "unknown" (``complete`` False) and the caller
keeps those objects. For an administrator the scan reads every audit except the ones being
undone (``GET /api/audits/:id``: company, client, findings and sections), the contacts' company
links (``GET /api/clients``) and the vulnerability database (``GET /api/vulnerabilities``, whose
entries may hold a finding's HTML with its ``<img>`` ids), and collects every 24-hex id they
contain. Any read failure, or more audits than ``MAX_SCAN_AUDITS``, gives "unknown" as well.
Nothing is logged but exception types.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..pwndoc_client import PwnDocClient, PwnDocError

log = logging.getLogger("pwndoc_ai.docx_import")

ADMIN_ROLE = "admin"
MAX_SCAN_AUDITS = 2000
OBJECT_ID_TOKEN = re.compile(r"(?<![0-9a-f])[0-9a-f]{24}(?![0-9a-f])")


@dataclass(frozen=True)
class Usage:
    """``complete`` is True only when every object that could reference ours was read."""

    complete: bool = False
    referenced: frozenset[str] = frozenset()

    def in_use(self, object_id: str) -> bool:
        """True when ``object_id`` is referenced elsewhere, or when that cannot be known."""
        return not self.complete or object_id in self.referenced


def ref_id(value: Any) -> str:
    """An id given either as a string or as a populated ``{_id: ...}`` object."""
    if isinstance(value, dict):
        value = value.get("_id")
    return value if isinstance(value, str) else ""


def _tokens(value: Any) -> set[str]:
    text = json.dumps(value, ensure_ascii=True, default=str).lower()
    return set(OBJECT_ID_TOKEN.findall(text))


def sees_everything(client: PwnDocClient) -> bool:
    """True for an administrator (PwnDoc then lists every audit)."""
    me = client.me()
    return isinstance(me, dict) and me.get("role") == ADMIN_ROLE


def _audit_tokens(client: PwnDocClient, audit_ids: Iterable[str]) -> set[str]:
    found: set[str] = set()
    for audit_id in audit_ids:
        try:
            found |= _tokens(client.get_audit(audit_id))
        except PwnDocError as exc:
            if exc.status != 404:  # deleted meanwhile: it uses nothing any more
                raise
    return found


def _contact_tokens(client: PwnDocClient, skip: frozenset[str]) -> set[str]:
    rows = client.list_clients() or []
    links = [r.get("company") for r in rows if isinstance(r, dict) and ref_id(r) not in skip]
    return _tokens(links)


def _library_tokens(client: PwnDocClient) -> set[str]:
    found = _tokens(client.list_vulnerabilities())
    for row in client.list_languages() or []:
        locale = row.get("locale") if isinstance(row, dict) else None
        if isinstance(locale, str):
            found |= _tokens(client.list_vulnerabilities_by_locale(locale))
    return found


def scan_usage(
    client: PwnDocClient, *, skip_audits: frozenset[str], skip_clients: frozenset[str]
) -> Usage:
    """The ids referenced by every audit outside ``skip_audits``, by the contacts outside
    ``skip_clients`` and by the vulnerability library; ``Usage()`` (unknown) when that cannot
    be read in full."""
    try:
        if not sees_everything(client):
            return Usage()
        rows = client.list_audits() or []
        ids = [ref_id(r) for r in rows if isinstance(r, dict)]
        if len(ids) > MAX_SCAN_AUDITS:
            return Usage()
        found = _audit_tokens(client, (i for i in ids if i and i not in skip_audits))
        found |= _contact_tokens(client, skip_clients)
        found |= _library_tokens(client)
    except (PwnDocError, ValueError) as exc:
        log.warning("import undo: usage check failed (%s)", type(exc).__name__)
        return Usage()
    return Usage(complete=True, referenced=frozenset(found))


__all__ = ["MAX_SCAN_AUDITS", "Usage", "ref_id", "scan_usage", "sees_everything"]
