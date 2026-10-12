"""Resumable file-based pipeline: findings JSON -> LLM refinement -> JSON -> PwnDoc upload.

``refine_file`` writes the output after *every* finding (atomically), so an interrupted or
partially failed run can simply be started again: findings already present in the output file
are skipped.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .jsonio import finding_keys, read_findings, read_findings_or_empty, write_json_atomic
from .llm import Usage
from .pwndoc_client import PwnDocError

if TYPE_CHECKING:
    from .pwndoc_client import PwnDocClient

log = logging.getLogger(__name__)

STATUS_REFINED = "refined"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_UPLOADED = "uploaded"


@dataclass(frozen=True)
class ProgressEvent:
    index: int
    total: int
    title: str
    status: str
    message: str = ""


ProgressFn = Callable[[ProgressEvent], None]


@dataclass(frozen=True)
class PipelineSummary:
    total: int
    skipped: int
    refined: int
    failed: tuple[tuple[str, str], ...] = ()  # (finding key, error message)
    usage: Usage = field(default_factory=Usage)


def _merge(finding: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from .refiner import apply_payload

    return apply_payload(finding, payload)


def _update_payload(finding: dict[str, Any]) -> dict[str, Any]:
    from .refiner import finding_update_payload

    return finding_update_payload(finding)


def _notify(callback: ProgressFn | None, event: ProgressEvent) -> None:
    if callback is not None:
        callback(event)


def write_usage_summary(
    directory: Path, usage: Usage, per_finding: list[dict[str, Any]], stamp: str | None = None
) -> Path:
    stamp = stamp or time.strftime("%Y%m%d_%H%M%S")
    path = directory / f"tokens_used_{stamp}.json"
    write_json_atomic(
        path,
        {
            "timestamp": stamp,
            "total_input_tokens": usage.input_tokens,
            "total_output_tokens": usage.output_tokens,
            "total_tokens": usage.total,
            "per_finding": per_finding,
        },
    )
    return path


def _load_done(out_path: str | Path) -> dict[str, dict[str, Any]]:
    previous = read_findings_or_empty(out_path)
    return dict(zip(finding_keys(previous), previous, strict=True))


def _usage_row(key: str, title: str, usage: Usage) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total,
    }


def refine_file(
    in_path: str | Path,
    out_path: str | Path,
    refiner: Any,
    options: Any,
    on_progress: ProgressFn | None = None,
    write_usage_file: bool = True,
) -> PipelineSummary:
    """Refine every finding in ``in_path`` into ``out_path``; resume-safe and failure-isolated."""
    findings = read_findings(in_path)
    done = _load_done(out_path)
    keys = finding_keys(findings)
    total = len(findings)
    skipped = refined = 0
    failed: list[tuple[str, str]] = []
    usage = Usage()
    per_finding: list[dict[str, Any]] = []

    for index, finding in enumerate(findings, 1):
        key, title = keys[index - 1], str(finding.get("title", ""))
        if key in done:
            skipped += 1
            _notify(
                on_progress, ProgressEvent(index, total, title, STATUS_SKIPPED, "already refined")
            )
            continue
        try:
            proposal = refiner.refine(finding, options)
            done[key] = _merge(finding, proposal.payload)
        except Exception as exc:  # noqa: BLE001 - one bad finding must not abort the batch
            log.warning("refining %r failed: %s", title, exc)
            failed.append((key, str(exc)))
            _notify(on_progress, ProgressEvent(index, total, title, STATUS_FAILED, str(exc)))
            continue
        write_json_atomic(out_path, list(done.values()))
        refined += 1
        usage = usage + proposal.usage
        per_finding.append(_usage_row(key, title, proposal.usage))
        _notify(on_progress, ProgressEvent(index, total, title, STATUS_REFINED))

    if write_usage_file and refined:
        write_usage_summary(Path(out_path).resolve().parent, usage, per_finding)
    return PipelineSummary(total, skipped, refined, tuple(failed), usage)


def upload_file(
    path: str | Path,
    client: PwnDocClient,
    audit_id: str,
    on_progress: ProgressFn | None = None,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """PUT each finding of ``path`` whose ``_id`` exists in ``audit_id``.

    Returns ``(uploaded_ids, ((id_or_title, reason), ...))``. Findings that are not part of the
    target audit are reported and skipped, never created.
    """
    findings = read_findings(path)
    existing = {str(f.get("_id")) for f in client.list_findings(audit_id)}
    ok: list[str] = []
    failed: list[tuple[str, str]] = []
    total = len(findings)
    for index, finding in enumerate(findings, 1):
        title = str(finding.get("title", ""))
        fid = str(finding.get("_id") or "")
        label = fid or title
        if not fid or fid not in existing:
            reason = "not part of the target audit"
        else:
            try:
                client.update_finding(audit_id, fid, _update_payload(finding))
            except PwnDocError as exc:
                reason = str(exc)
            else:
                ok.append(fid)
                _notify(on_progress, ProgressEvent(index, total, title, STATUS_UPLOADED))
                continue
        failed.append((label, reason))
        _notify(on_progress, ProgressEvent(index, total, title, STATUS_FAILED, reason))
    return tuple(ok), tuple(failed)
