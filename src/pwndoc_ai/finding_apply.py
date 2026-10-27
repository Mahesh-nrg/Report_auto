"""Write a reviewed normalisation plan to PwnDoc as one reversible operation.

Order of operations (docs/FINDING_ORDER.md, "Finish / Normalize"):

1. hold the audit exclusively (no AI job, restore or other normalisation can overlap);
2. re-read the live findings and rebuild the plan; refuse when it differs from what the user
   reviewed (``expected_digest``);
3. write the automatic backup (the Restore feature lists it);
4. PUT every finding's changed keys, then move findings into report order (``movefinding``);
5. re-read and verify. Any failure in step 4 restores the backup and the previous order.

PwnDoc's REST API cannot change a finding's ``identifier``: outputs number findings by ``seq``
(the template's ``sortFindings`` filter and the gateway views), so stored identifiers stay as they
are and the verification reports them as a warning, never as a failure.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .finding_review import Normalizer, PlanRow, build_plan, needs_write, plan_digest, verification
from .history import restore_backup, write_backup
from .pwndoc_client import safe_id

log = logging.getLogger(__name__)
PLAN_CHANGED = "the findings changed since you reviewed the plan; review it again"


class ApplyError(RuntimeError):
    """The write failed; ``restored`` tells whether the backup was put back."""

    def __init__(self, message: str, restored: bool = False, backup: str = ""):
        super().__init__(message)
        self.restored = restored
        self.backup = backup


class PlanChanged(ValueError):
    """The live audit no longer matches the plan the user approved."""


def _ids(findings: list[Mapping[str, Any]]) -> list[str]:
    return [str(f.get("_id") or f.get("id") or "") for f in findings if isinstance(f, Mapping)]


def _move(client: Any, audit_id: str, old: int, new: int) -> None:
    mover = getattr(client, "move_finding", None)
    if callable(mover):
        mover(audit_id, old, new)
        return
    path = f"/api/audits/{safe_id(audit_id, 'audit')}/movefinding"
    client._request("PUT", path, json={"oldIndex": old, "newIndex": new})  # noqa: SLF001


def apply_order(client: Any, audit_id: str, wanted: list[str]) -> bool:
    """Move findings until the stored order is ``wanted``; True when PwnDoc confirms it."""
    current = [i for i in _ids(client.list_findings(audit_id)) if i in wanted]
    for target, finding_id in enumerate(wanted):
        if finding_id not in current or current[target] == finding_id:
            continue
        old = current.index(finding_id)
        _move(client, audit_id, old, target)
        current = [*current[:old], *current[old + 1 :]]
        current = [*current[:target], finding_id, *current[target:]]
    after = [i for i in _ids(client.list_findings(audit_id)) if i in wanted]
    return after == [i for i in wanted if i in after]


def _write_fields(client: Any, audit_id: str, plan: list[PlanRow]) -> int:
    written = 0
    for row in plan:
        if row.payload:
            client.update_finding(audit_id, row.finding_id, dict(row.payload))
            written += 1
    return written


def _rollback(data_dir: Path, audit_id: str, client: Any, backup: str, order: list[str]) -> bool:
    try:
        restore_backup(data_dir, audit_id, backup, client)
        apply_order(client, audit_id, order)
        return True
    except Exception as exc:  # noqa: BLE001 - the caller reports the original failure
        log.warning("normalise: rollback of audit %s failed (%s)", audit_id, type(exc).__name__)
        return False


def apply_plan(
    data_dir: str | Path,
    audit_id: str,
    client: Any,
    jobs: Any,
    normalizer: Normalizer | None = None,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    """Write the plan behind a backup and the per-audit lock (``jobs.exclusive``)."""
    with jobs.exclusive(audit_id):  # JobConflict when something else holds the audit
        live = client.list_findings(audit_id)
        plan = build_plan(live, normalizer)
        if expected_digest and plan_digest(plan) != expected_digest:
            raise PlanChanged(PLAN_CHANGED)
        if not needs_write(plan):
            return {"backup": "", "written": 0, "order_applied": True,
                    "report": verification(audit_id, plan)}  # fmt: skip
        backup = write_backup(data_dir, audit_id, live)
        try:
            written = _write_fields(client, audit_id, plan)
            ordered = apply_order(client, audit_id, [r.finding_id for r in plan])
        except Exception as exc:
            restored = _rollback(Path(data_dir), audit_id, client, backup, _ids(live))
            raise ApplyError("writing the normalised findings failed", restored, backup) from exc
        after = build_plan(client.list_findings(audit_id), normalizer)
    return {
        "backup": backup,
        "written": written,
        "order_applied": ordered,
        "report": verification(audit_id, after),
    }
