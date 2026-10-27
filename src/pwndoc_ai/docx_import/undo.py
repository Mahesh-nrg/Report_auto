"""Undo an import: delete exactly the PwnDoc objects its journal lists, nothing else.

First a check: when a journaled audit holds a finding the import did not create (written by
hand later), the undo refuses with ``ImportConflict("changed_since_import")`` and deletes
nothing. Then the order: the audits (PwnDoc deletes their findings with them, so findings need
no call of their own), then the contact, then the company, then the screenshots.

The contact, the company and the screenshots may be shared after the import: a later import
reuses a contact by e-mail and a screenshot by its bytes, and anyone may pick the company for
another audit. So each is deleted only when ``undo_usage.scan_usage`` read all of PwnDoc (an
administrator) and found no other audit, contact or library entry naming its id; otherwise it
is reported ``kept``. A screenshot is also kept when its row now belongs to another audit.
When an audit delete fails, nothing else is touched (``failed``, for a retry).

Outcomes: ``deleted``; ``gone`` for a 404 (already absent, so a retry is idempotent); ``kept``
as above and for a 403 on a screenshot; ``failed`` for 401, 5xx, connection errors and anything
else. Nothing here raises for PwnDoc problems; the result says what happened to every id.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from typing import Any

from ..pwndoc_client import PwnDocClient, PwnDocError
from .errors import ImportConflict
from .records import CreatedObject, Journal, UndoItem, UndoResult
from .undo_usage import Usage, ref_id, scan_usage

log = logging.getLogger("pwndoc_ai.docx_import")

OpenClient = Callable[[], AbstractContextManager[PwnDocClient]]
Progress = Callable[[str, int, int], None]

STEP_LABELS = {
    "audit": "Deleting the audit",
    "client": "Deleting the contact",
    "company": "Deleting the company",
    "image": "Deleting screenshots",
}
UNDO_ORDER = ("audit", "client", "company", "image")
DONE_OUTCOMES = frozenset({"deleted", "gone"})
SHARED_KINDS = frozenset({"client", "company", "image"})


def outcome_for(exc: Exception, kind: str) -> str:
    """Map a failed delete to an outcome (404 gone, 403 on an image kept, else failed)."""
    status = getattr(exc, "status", None)
    if status == 404:
        return "gone"
    if status == 403 and kind == "image":
        return "kept"
    return "failed"


def _attempt(kind: str, call: Callable[[], Any]) -> str:
    try:
        call()
    except (PwnDocError, ValueError) as exc:
        log.warning("import undo: %s delete failed (%s)", kind, type(exc).__name__)
        return outcome_for(exc, kind)
    return "deleted"


def _of_kind(journal: Journal, kind: str) -> tuple[CreatedObject, ...]:
    return tuple(o for o in journal.objects if o.kind == kind)


def _check_unchanged(client: PwnDocClient, journal: Journal) -> None:
    """Refuse (``changed_since_import``) when a journaled audit now holds a finding the import
    did not create: deleting the audit would delete that finding with it."""
    for audit in _of_kind(journal, "audit"):
        try:
            row = client.get_audit(audit.id)
        except PwnDocError as exc:
            if exc.status == 404:
                continue  # already gone: nothing of anyone's left in it
            raise
        rows = row.get("findings") if isinstance(row, dict) else None
        found = {ref_id(f) for f in rows or [] if isinstance(f, dict)}
        ours = {f.id for f in _of_kind(journal, "finding") if f.audit_id == audit.id}
        if found - ours:
            raise ImportConflict("changed_since_import")


def _delete_shared(kind: str, object_id: str, usage: Usage, call: Callable[[], Any]) -> str:
    """Delete a journaled company or contact only when nothing else is known to use it."""
    if usage.in_use(object_id):
        return "kept"  # someone else's data may depend on it (or PwnDoc cannot tell us)
    return _attempt(kind, call)


def _delete_image(client: PwnDocClient, item: CreatedObject, usage: Usage) -> str:
    try:
        row = client.get_image(item.id)
    except (PwnDocError, ValueError) as exc:
        return outcome_for(exc, "image")
    if ref_id(row.get("auditId")) != item.audit_id or usage.in_use(item.id):
        return "kept"  # attached to, or shown by, another audit: not ours to delete
    return _attempt("image", lambda: client.delete_image(item.id))


def _undo_audits(client: PwnDocClient, journal: Journal, tick: Callable[[str], None]) -> list:
    items: list[UndoItem] = []
    for audit in _of_kind(journal, "audit"):
        tick("audit")
        outcome = _attempt("audit", lambda a=audit: client.delete_audit(a.id))
        items.append(UndoItem("audit", audit.id, outcome))
        finding_outcome = outcome if outcome in DONE_OUTCOMES else "failed"
        items.extend(
            UndoItem("finding", f.id, finding_outcome)
            for f in _of_kind(journal, "finding")
            if f.audit_id == audit.id
        )
    owned = {a.id for a in _of_kind(journal, "audit")}
    items.extend(  # a finding whose audit is not journaled is never touched
        UndoItem("finding", f.id, "failed")
        for f in _of_kind(journal, "finding")
        if f.audit_id not in owned
    )
    return items


def _usage(client: PwnDocClient, journal: Journal) -> Usage:
    if not any(o.kind in SHARED_KINDS for o in journal.objects):
        return Usage()
    return scan_usage(
        client,
        skip_audits=frozenset(o.id for o in _of_kind(journal, "audit")),
        skip_clients=frozenset(o.id for o in _of_kind(journal, "client")),
    )


def _undo_shared(client: PwnDocClient, journal: Journal, tick: Callable[[str], None]) -> list:
    """The contact, the company and the screenshots, once the audits are gone."""
    usage = _usage(client, journal)
    calls = {
        "client": lambda o: _delete_shared(
            "client", o.id, usage, lambda: client.delete_client(o.id)
        ),
        "company": lambda o: _delete_shared(
            "company", o.id, usage, lambda: client.delete_company(o.id)
        ),
        "image": lambda o: _delete_image(client, o, usage),
    }
    items = []
    for kind in UNDO_ORDER[1:]:
        for item in _of_kind(journal, kind):
            tick(kind)
            items.append(UndoItem(kind, item.id, calls[kind](item)))
    return items


def _run(client: PwnDocClient, journal: Journal, progress: Progress) -> list[UndoItem]:
    total = sum(1 for o in journal.objects if o.kind != "finding")
    done = [0]

    def tick(kind: str) -> None:
        progress(STEP_LABELS[kind], done[0], total)
        done[0] += 1

    items = _undo_audits(client, journal, tick)
    if any(i.kind == "audit" and i.outcome not in DONE_OUTCOMES for i in items):
        # our audit may still reference them: keep them all for a retry
        for kind in UNDO_ORDER[1:]:
            items.extend(UndoItem(kind, o.id, "failed") for o in _of_kind(journal, kind))
    else:
        items.extend(_undo_shared(client, journal, tick))
    progress(STEP_LABELS["image"], total, total)
    return items


def _all_failed(journal: Journal) -> tuple[UndoItem, ...]:
    return tuple(UndoItem(o.kind, o.id, "failed") for o in _ordered(journal.objects))


def _ordered(objects: Sequence[CreatedObject]) -> tuple[CreatedObject, ...]:
    rank = {kind: index for index, kind in enumerate(("audit", "finding", *UNDO_ORDER[1:]))}
    return tuple(sorted(objects, key=lambda o: rank[o.kind]))


def undo_import(
    journal: Journal, *, open_client: OpenClient, progress: Progress, check_changes: bool = True
) -> UndoResult:
    """Delete the journaled ids in the safe order; every journaled id gets one ``UndoItem``.

    With ``check_changes`` (every undo the user asks for) an audit that gained findings since
    the import raises ``ImportConflict("changed_since_import")`` before anything is deleted.
    The writer's own rollback passes False: its audit is minutes old and its findings are
    journaled only once they are all created.
    """
    if not journal.objects:
        return UndoResult()
    try:
        with open_client() as client:
            if check_changes:
                _check_unchanged(client, journal)
            items = _run(client, journal, progress)
    except (PwnDocError, ValueError, OSError) as exc:
        log.warning("import undo: cannot reach PwnDoc (%s)", type(exc).__name__)
        return UndoResult(_all_failed(journal))
    return UndoResult(tuple(items))


def fully_undone(result: UndoResult) -> bool:
    """True when nothing is left that a retry could still delete (``kept`` cannot be)."""
    return all(item.outcome != "failed" for item in result.items)
