"""Run history and restore.

* :class:`RunLog` appends one JSON line per job event to ``<data_dir>/ai-log.jsonl`` and merges
  those events back into one record per job. Events never contain finding text; every string is
  capped so the log cannot grow without bound. Recording never raises.
* :func:`list_backups` / :func:`restore_backup` expose the pre-change snapshots that
  ``jobs.py`` writes to ``<data_dir>/backups/<audit_id>/``. A restore first snapshots the *current*
  live findings in the same format, so a restore can itself be undone. A restore sends every
  field the backup knows, including empty ones, so text an AI run added to a previously empty
  field is removed again. The snapshot is deleted when the restore changed nothing, and
  :func:`prune_backups` keeps the folder bounded.

Event schema understood by :meth:`RunLog.runs_for_audit` (all keys optional except ``job_id``):
``event`` ('start' | 'progress' | 'end'), ``job_id``, ``audit_id``, ``user``, ``kind``
('update' | 'finish'), ``ts``, ``status`` (on 'end': done | error | cancelled | interrupted),
``model``/``models``, ``tokens`` (int), ``applied``/``failed`` (int), ``report`` (bool), ``error``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .jsonio import write_json_atomic
from .refiner import UPDATABLE_KEYS

log = logging.getLogger(__name__)

LOG_NAME = "ai-log.jsonl"
DEFAULT_MAX_BYTES = 5_000_000
MAX_STRING = 500
MAX_FIELD_JSON = 2_000
MAX_KEYS = 50
MAX_ITEMS = 50
MAX_DEPTH = 3
MAX_LINE_BYTES = 64_000
MAX_RUNS = 500
MAX_BACKUPS_LISTED = 50  # only the newest are parsed for the count
MAX_BACKUPS_KEPT = 100  # older snapshots of one audit are deleted when a new one is written
MAX_BACKUP_BYTES = 64_000_000
MAX_COUNT_CACHE = 500
TEXT_KEYS = ("description", "observation", "remediation", "poc", "scope")
LIST_KEYS = ("references", "customFields")
MAX_ID_LEN = 128
KINDS = ("update", "finish")
END_STATUSES = ("done", "error", "cancelled", "interrupted")

AUDIT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
BACKUP_NAME_RE = re.compile(r"(\d{8})T(\d{6})(\d{0,6})Z_[0-9a-f]{8}\.json", re.ASCII)
_STAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)


class BackupError(ValueError):
    """The backup name or audit id is unsafe, or the backup is missing or unreadable."""


class FindingsClient(Protocol):
    def list_findings(self, audit_id: str) -> list[dict[str, Any]]: ...

    def update_finding(self, audit_id: str, finding_id: str, payload: dict[str, Any]) -> Any: ...


# -- event hygiene -----------------------------------------------------------------------------
def _clean_value(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:MAX_STRING]
    if depth < MAX_DEPTH and isinstance(value, Mapping):
        items = list(value.items())[:MAX_KEYS]
        return {str(k)[:100]: _clean_value(v, depth + 1) for k, v in items}
    if depth < MAX_DEPTH and isinstance(value, list | tuple | set | frozenset):
        return [_clean_value(v, depth + 1) for v in list(value)[:MAX_ITEMS]]
    return str(value)[:MAX_STRING]


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _clean_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """A bounded copy of the event: capped strings, oversized fields dropped, ``ts`` added."""
    cleaned: dict[str, Any] = {}
    for key, value in list(event.items())[:MAX_KEYS]:
        item = _clean_value(value)
        if _json_size(item) <= MAX_FIELD_JSON:
            cleaned[str(key)[:100]] = item
    if not isinstance(cleaned.get("ts"), str) or not cleaned["ts"]:
        cleaned["ts"] = datetime.now(timezone.utc).isoformat()
    return cleaned


def _make_private_dirs(directory: Path) -> None:
    missing = [d for d in (directory, *directory.parents) if not d.exists()]
    for folder in reversed(missing):
        folder.mkdir(mode=0o700, exist_ok=True)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        view = view[written:]


class RunLog:
    """Append-only JSON-lines log of job events, rotated to ``ai-log.jsonl.1`` when full."""

    def __init__(self, data_dir: str | Path, max_bytes: int = DEFAULT_MAX_BYTES):
        self._dir = Path(data_dir)
        self._path = self._dir / LOG_NAME
        self._rotated = self._dir / f"{LOG_NAME}.1"
        self._max_bytes = max(1, int(max_bytes))
        self._lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._cache: tuple[tuple[Any, ...], dict[str, dict[str, Any]]] | None = None

    def record(self, event: Mapping[str, Any]) -> None:
        """Append one event. Never raises; a failure leaves a warning (with no event content)."""
        try:
            if not isinstance(event, Mapping):
                log.warning("run log: ignored an event that is not a mapping")
                return
            line = json.dumps(_clean_event(event), ensure_ascii=False, default=str) + "\n"
            self._append(line.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - history must never break a job
            log.warning("run log: could not record event (%s)", type(exc).__name__)

    def _append(self, data: bytes) -> None:
        with self._lock:
            _make_private_dirs(self._dir)
            self._rotate_if_needed(len(data))
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | _NO_FOLLOW
            descriptor = os.open(self._path, flags, 0o600)
            try:
                _write_all(descriptor, data)
            finally:
                os.close(descriptor)

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self._path.stat().st_size
        except FileNotFoundError:
            return
        if size > 0 and size + incoming > self._max_bytes:
            os.replace(self._path, self._rotated)

    # -- reading -------------------------------------------------------------------------------
    def _events(self) -> Iterator[dict[str, Any]]:
        for path in (self._rotated, self._path):
            try:
                handle = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115
            except OSError:
                continue
            with handle:
                for raw in handle:
                    event = _parse_line(raw)
                    if event is not None:
                        yield event

    def _signature(self) -> tuple[Any, ...]:
        """(mtime, size, inode) of both log files: any append or rotation changes it."""
        parts: list[Any] = []
        for path in (self._rotated, self._path):
            try:
                info = path.stat()
                parts.append((info.st_mtime_ns, info.st_size, info.st_ino))
            except OSError:
                parts.append(None)
        return tuple(parts)

    def _states(self) -> dict[str, dict[str, Any]]:
        """Merged state of every job in the log, re-parsed only when the files changed."""
        signature = self._signature()  # taken first: a concurrent append can only force a reparse
        with self._cache_lock:
            if self._cache is not None and self._cache[0] == signature:
                return self._cache[1]
        states: dict[str, dict[str, Any]] = {}
        for order, event in enumerate(self._events()):
            job_id = event["job_id"]
            states[job_id] = _merge_event(states.get(job_id) or _new_state(job_id, order), event)
        with self._cache_lock:
            self._cache = (signature, states)
        return states

    def runs_for_audit(
        self, audit_id: str, limit: int = 50, live_job_ids: Collection[str] = ()
    ) -> list[dict[str, Any]]:
        """One merged record per job for this audit, newest first."""
        if not isinstance(audit_id, str) or not AUDIT_ID_RE.fullmatch(audit_id):
            return []
        count = max(0, min(int(limit), MAX_RUNS))
        if count == 0:
            return []
        live = {str(j) for j in live_job_ids}
        mine = [s for s in self._states().values() if s["audit_id"] == audit_id]
        mine.sort(key=lambda s: (_ts_key(s["started"] or s["first_ts"]), s["_order"]), reverse=True)
        return [_finish_state(s, live) for s in mine[:count]]


def _parse_line(raw: str) -> dict[str, Any] | None:
    if len(raw) > MAX_LINE_BYTES:
        return None
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    job_id = event.get("job_id")
    if not isinstance(job_id, str) or not job_id or len(job_id) > MAX_ID_LEN:
        return None
    return event


def _ts_key(value: Any) -> float:
    if not isinstance(value, str):
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def _new_state(job_id: str, order: int) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "audit_id": "",
        "user": "",
        "kind": "",
        "started": None,
        "ended": None,
        "first_ts": None,
        "end_status": None,
        "models": [],
        "tokens": 0,
        "applied": 0,
        "failed": 0,
        "report": False,
        "error": "",
        "_order": order,
    }


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _models_of(event: Mapping[str, Any]) -> list[str]:
    found = [event.get("model")]
    extra = event.get("models")
    if isinstance(extra, list):
        found.extend(extra)
    return [m[:MAX_ID_LEN] for m in found if isinstance(m, str) and m]


def _merge_event(state: dict[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    """A new state with this event folded in (later events win; models accumulate)."""
    ts = event.get("ts") if isinstance(event.get("ts"), str) else None
    kind = event.get("event")
    new = {**state}
    new["first_ts"] = state["first_ts"] or ts
    if kind == "start":
        new["started"] = ts
    elif kind == "end":
        new["ended"] = ts
        status = event.get("status")
        new["end_status"] = status if status in END_STATUSES else "done"
    for key in ("audit_id", "user"):
        if isinstance(event.get(key), str) and event[key]:
            new[key] = event[key][:MAX_ID_LEN]
    if event.get("kind") in KINDS:
        new["kind"] = event["kind"]
    for key in ("tokens", "applied", "failed"):
        number = _count(event.get(key))
        if number is not None:
            new[key] = number
    if isinstance(event.get("report"), bool):
        new["report"] = event["report"]
    if isinstance(event.get("error"), str):
        new["error"] = event["error"][:MAX_STRING]
    seen = _models_of(event)
    if seen:
        new["models"] = [
            *state["models"],
            *[m for m in dict.fromkeys(seen) if m not in state["models"]],
        ]
        new["model"] = seen[-1]
    return new


def _finish_state(state: Mapping[str, Any], live: Collection[str]) -> dict[str, Any]:
    if state["end_status"]:
        status = state["end_status"]
    else:
        status = "running" if state["job_id"] in live else "interrupted"
    return {
        "job_id": state["job_id"],
        "audit_id": state["audit_id"],
        "user": state["user"],
        "kind": state["kind"],
        "started": state["started"] or state["first_ts"],
        "ended": state["ended"],
        "status": status,
        "model": state.get("model", ""),
        "models": list(state["models"]),
        "tokens": state["tokens"],
        "applied": state["applied"],
        "failed": state["failed"],
        "report": state["report"],
        "error": state["error"],
    }


# -- backups -----------------------------------------------------------------------------------
def _backup_dir(data_dir: str | Path, audit_id: str) -> Path:
    if not isinstance(audit_id, str) or not AUDIT_ID_RE.fullmatch(audit_id):
        raise BackupError("invalid audit id")
    return Path(data_dir) / "backups" / audit_id


def _backup_path(data_dir: str | Path, audit_id: str, name: str) -> Path:
    """The backup file for ``name``, proven to live directly inside the audit's backup folder."""
    folder = _backup_dir(data_dir, audit_id)
    if not isinstance(name, str) or not BACKUP_NAME_RE.fullmatch(name):
        raise BackupError("invalid backup name")
    resolved = (folder / name).resolve()
    if resolved.parent != folder.resolve() or resolved.name != name:
        raise BackupError("backup is outside the backup folder")
    if not resolved.is_file():
        raise BackupError("backup not found")
    return resolved


def _load_backup(path: Path) -> list[dict[str, Any]]:
    try:
        if path.stat().st_size > MAX_BACKUP_BYTES:
            raise BackupError("backup is too large")
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except OSError as exc:
        raise BackupError(f"backup could not be read ({type(exc).__name__})") from exc
    except ValueError as exc:
        raise BackupError("backup is not valid JSON") from exc
    if not isinstance(data, list):
        raise BackupError("backup is not a list of findings")
    return [item for item in data if isinstance(item, dict)]


def _created_from_name(name: str, path: Path) -> str:
    match = BACKUP_NAME_RE.fullmatch(name)
    try:
        if match is None:
            raise ValueError(name)
        day, clock, micro = match.groups()
        moment = datetime.strptime(day + clock + micro.ljust(6, "0"), "%Y%m%d%H%M%S%f")
        return moment.replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


_COUNTS: dict[tuple[str, int, int], int] = {}
_COUNTS_LOCK = threading.Lock()


def _finding_count(path: Path) -> int:
    """Findings in a backup; parsed once per (file, size, mtime) so listing stays cheap."""
    info = path.stat()
    key = (str(path), info.st_size, info.st_mtime_ns)
    with _COUNTS_LOCK:
        if key in _COUNTS:
            return _COUNTS[key]
    count = len(_load_backup(path))
    with _COUNTS_LOCK:
        while len(_COUNTS) >= max(1, MAX_COUNT_CACHE):
            _COUNTS.pop(next(iter(_COUNTS)))  # oldest first; the rest stay warm
        _COUNTS[key] = count
    return count


def _backup_names(folder: Path) -> list[str]:
    """Names that look like backups, newest first (the timestamp leads the name)."""
    return sorted(
        (p.name for p in folder.iterdir() if BACKUP_NAME_RE.fullmatch(p.name)), reverse=True
    )


def list_backups(data_dir: str | Path, audit_id: str) -> list[dict[str, Any]]:
    """The newest readable backups of an audit, newest first: ``{name, created, findings}``."""
    try:
        folder = _backup_dir(data_dir, audit_id)
        names = _backup_names(folder)
    except (BackupError, OSError):
        return []
    rows: list[dict[str, Any]] = []
    for name in names:
        if len(rows) >= MAX_BACKUPS_LISTED:
            break
        try:
            path = _backup_path(data_dir, audit_id, name)
            rows.append(
                {
                    "name": name,
                    "created": _created_from_name(name, path),
                    "findings": _finding_count(path),
                }
            )
        except (BackupError, OSError):
            continue
    rows.sort(key=lambda r: (r["created"], r["name"]), reverse=True)
    return rows


def prune_backups(data_dir: str | Path, audit_id: str, keep: int | None = None) -> int:
    """Delete the oldest backups of an audit beyond ``keep``; returns how many were removed."""
    limit = MAX_BACKUPS_KEPT if keep is None else keep
    try:
        folder = _backup_dir(data_dir, audit_id)
        names = _backup_names(folder)
    except (BackupError, OSError):
        return 0
    removed = 0
    for name in names[max(limit, 1) :]:
        try:
            (folder / name).unlink()
            removed += 1
        except OSError as exc:
            log.warning("could not prune backup %s: %s", name, type(exc).__name__)
    return removed


# -- restore -----------------------------------------------------------------------------------
# Keys PwnDoc ignores when they arrive blank: an AI-added value in them cannot be cleared again.
BLANK_IGNORED_KEYS = (
    "title", "vulnType", "cvssv3", "cvssv4", "priority", "remediationComplexity", "status",
    "category",
)  # fmt: skip
EDITED_SINCE = "edited in PwnDoc since the restore started"
NOT_FOUND = "not found in PwnDoc any more"


@dataclass(frozen=True)
class RestoreResult:
    restored: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]
    safety_backup: str
    notes: tuple[tuple[str, str], ...] = ()  # (finding id, what could not be reverted)


@dataclass(frozen=True)
class _Outcome:
    reason: str | None = None  # why the finding was skipped
    note: str | None = None  # restored, but with a caveat


def _finding_id(finding: Mapping[str, Any]) -> str:
    return str(finding.get("_id") or finding.get("id") or "")


def _write_safety_backup(data_dir: str | Path, audit_id: str, live: list[Any]) -> str:
    """Snapshot the current live findings in the same format ``jobs.py`` uses."""
    stamp = datetime.now(timezone.utc).strftime(_STAMP_FORMAT)
    name = f"{stamp}_{uuid.uuid4().hex[:8]}.json"
    path = _backup_dir(data_dir, audit_id) / name
    try:
        write_json_atomic(path, [f for f in live if isinstance(f, dict)])
    except OSError as exc:
        raise BackupError("could not write the safety backup, nothing restored") from exc
    return name


def write_backup(data_dir: str | Path, audit_id: str, live: list[Any]) -> str:
    """Back up the live findings (stored order kept) before a gateway write; returns its name.

    Same format as the jobs' and restore's backups, so the Restore feature lists and reverts it.
    """
    name = _write_safety_backup(data_dir, audit_id, live)
    prune_backups(data_dir, audit_id)
    return name


def restore_payload(finding: Mapping[str, Any]) -> dict[str, Any]:
    """What to PUT so the live finding equals the backed-up one.

    PwnDoc only overwrites the keys it receives, so an empty text field is sent as ``""`` and an
    empty list as ``[]`` (it accepts both). ``finding_update_payload`` would drop them, leaving
    AI text that filled a formerly empty field in place. Fields PwnDoc ignores when blank (title,
    cvss, priority...) are only sent when the backup has a value.
    """
    payload: dict[str, Any] = {}
    for key in UPDATABLE_KEYS:
        value = finding.get(key)
        if key in TEXT_KEYS:
            payload[key] = value if isinstance(value, str) else ""
        elif key in LIST_KEYS:
            payload[key] = value if isinstance(value, list) else []
        elif value not in (None, ""):
            payload[key] = value
    return payload


def _skip_reason(exc: Exception) -> str:
    """A short reason that never echoes the exception text (it may contain finding content)."""
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return f"PwnDoc error {status}"
    return f"error: {type(exc).__name__}"


def _editable_view(finding: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a finding a restore writes; two equal views mean nobody edited it."""
    return {key: finding.get(key) for key in UPDATABLE_KEYS}


def _unreverted_keys(backup: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """Blank in the backup but filled now, in a field PwnDoc will not clear."""
    return [
        key
        for key in BLANK_IGNORED_KEYS
        if backup.get(key) in (None, "") and current.get(key) not in (None, "")
    ]


def _find_live(client: FindingsClient, audit_id: str, finding_id: str) -> Mapping[str, Any] | None:
    for item in client.list_findings(audit_id):
        if isinstance(item, dict) and _finding_id(item) == finding_id:
            return item
    return None


def _restore_one(
    client: FindingsClient,
    audit_id: str,
    finding: Mapping[str, Any],
    snapshot: Mapping[str, Mapping[str, Any]],
) -> _Outcome:
    """Write one finding back unless it changed since the safety snapshot was taken."""
    finding_id = _finding_id(finding)
    if not finding_id:
        return _Outcome("no id in the backup")
    if finding_id not in snapshot:
        return _Outcome(NOT_FOUND)
    try:
        current = _find_live(client, audit_id, finding_id)  # read right before the PUT
        if current is None:
            return _Outcome(NOT_FOUND)
        if _editable_view(current) != _editable_view(snapshot[finding_id]):
            return _Outcome(EDITED_SINCE)
        client.update_finding(audit_id, finding_id, restore_payload(finding))
    except Exception as exc:  # noqa: BLE001 - one bad finding must not stop the rest
        if getattr(exc, "status", None) == 401:
            raise
        return _Outcome(_skip_reason(exc))
    stuck = _unreverted_keys(finding, current)
    note = f"not reverted: {', '.join(stuck)} (PwnDoc ignores blank values)" if stuck else None
    return _Outcome(note=note)


def _selected(backup: list[dict[str, Any]], wanted: set[str] | None) -> Iterator[dict[str, Any]]:
    """Backup findings to write: filtered by ``wanted`` and each id once."""
    done: set[str] = set()
    for finding in backup:
        finding_id = _finding_id(finding)
        if wanted is not None and finding_id not in wanted:
            continue
        if finding_id and finding_id in done:  # an id repeated in one backup is written once
            continue
        done.add(finding_id)
        yield finding


def _finish_restore(
    data_dir: str | Path,
    audit_id: str,
    safety: str,
    restored: list[str],
    skipped: list[tuple[str, str]],
    notes: list[tuple[str, str]],
) -> RestoreResult:
    if restored:
        prune_backups(data_dir, audit_id)
    else:  # nothing changed, so the snapshot is not needed (and must not evict real backups)
        (_backup_dir(data_dir, audit_id) / safety).unlink(missing_ok=True)
        safety = ""
    log.info(
        "restore: %d restored, %d skipped (safety backup %s)",
        len(restored),
        len(skipped),
        safety or "none",
    )
    return RestoreResult(tuple(restored), tuple(skipped), safety, tuple(notes))


def restore_backup(
    data_dir: str | Path,
    audit_id: str,
    name: str,
    client: FindingsClient,
    *,
    only: Collection[str] | None = None,
) -> RestoreResult:
    """Write a backup's findings back to PwnDoc, after snapshotting the current state first.

    Raises :class:`BackupError` for an unsafe/missing/corrupt backup (nothing is touched). Each
    finding is re-read right before its PUT and skipped when a human changed it since the
    snapshot. A 401 from PwnDoc is re-raised unchanged, with ``partial_restore`` (a
    :class:`RestoreResult`) attached so the caller knows what was already written. Other
    per-finding failures are returned in ``skipped``; values that cannot be cleared are in
    ``notes``.
    """
    backup = _load_backup(_backup_path(data_dir, audit_id, name))
    live = client.list_findings(audit_id)
    safety = _write_safety_backup(data_dir, audit_id, live)
    snapshot = {_finding_id(f): f for f in live if isinstance(f, dict) and _finding_id(f)}
    wanted = None if only is None else {str(i) for i in only}
    restored: list[str] = []
    skipped: list[tuple[str, str]] = []
    notes: list[tuple[str, str]] = []
    try:
        for finding in _selected(backup, wanted):
            finding_id = _finding_id(finding)
            outcome = _restore_one(client, audit_id, finding, snapshot)
            if outcome.reason is not None:
                skipped.append((finding_id[:64] or "?", outcome.reason))
                log.warning(
                    "restore: finding %s skipped (%s)", finding_id[:64] or "?", outcome.reason
                )
                continue
            restored.append(finding_id)
            if outcome.note:
                notes.append((finding_id[:64], outcome.note))
    except Exception as exc:  # in practice a 401: _restore_one reports every other failure
        partial = _finish_restore(data_dir, audit_id, safety, restored, skipped, notes)
        log.warning(
            "restore interrupted after %d finding(s) (%s)", len(partial.restored), _skip_reason(exc)
        )
        exc.partial_restore = partial  # type: ignore[attr-defined]
        raise
    return _finish_restore(data_dir, audit_id, safety, restored, skipped, notes)
