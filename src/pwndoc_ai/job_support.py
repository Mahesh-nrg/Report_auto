"""Texts, errors and pure helpers of the job manager.

Everything a job shows to a user is a fixed sentence from here: the detail of a failure (a
provider message, an internal address, a file path) goes to the gateway log, never into a job
view or a notification. ``jobs`` re-exports these names.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .llm import LlmAuthError, LlmError
from .pwndoc_client import SAFE_ID, PwnDocError

__all__ = [
    "ACTIVE", "APPLICABLE", "CHANGED_SINCE", "CUSTOM_PREFIX", "DEFAULT_LOCALE",
    "FINAL_ITEM_STATES", "SAFE_ID", "SESSION_EXPIRED", "UNSAVED", "AUDIT_BUSY", "AUDIT_LOCKED",
    "BACKUP_FAILED", "LLM_FAILED", "LLM_KEY_REJECTED", "PWNDOC_DENIED", "PWNDOC_FAILED",
    "PWNDOC_MISSING", "UNEXPECTED",
    "JobConflict", "JobError", "JobNotFound", "SessionExpiredError",
    "audit_language", "changed_fields", "clean_sources", "id_of", "user_message",
    "write_payload",
]  # fmt: skip

ACTIVE = ("queued", "running")
APPLICABLE = ("proposed", "failed")
FINAL_ITEM_STATES = ("applied", "failed", "skipped")

DEFAULT_LOCALE = "en"
SESSION_EXPIRED = "session expired: sign in to PwnDoc again, then apply manually"
CHANGED_SINCE = "changed in PwnDoc since the proposal; re-run to refresh"
CUSTOM_PREFIX = "customFields: "
UNSAVED = (
    "{n} finding{s} not saved to PwnDoc (your session expired), so no report was built. "
    "Sign in to PwnDoc again and run Finish report again."
)
PWNDOC_FAILED = "PwnDoc could not be reached or did not accept the request; see the gateway log"
PWNDOC_DENIED = "PwnDoc refused access to this audit"
PWNDOC_MISSING = "the audit or finding was not found in PwnDoc"
LLM_FAILED = "the AI model could not process this finding; see the gateway log"
LLM_KEY_REJECTED = "the AI provider rejected the API key; an administrator must check AI Settings"
BACKUP_FAILED = "could not write the backup, so nothing was applied; see the gateway log"
UNEXPECTED = "something went wrong; see the gateway log"
AUDIT_BUSY = "an AI job is running (or a restore is in progress) on this audit; try again shortly"
AUDIT_LOCKED = "this audit is being restored; try again shortly"


class JobError(RuntimeError):
    pass


class SessionExpiredError(JobError):
    """PwnDoc rejected the user's token; ``status`` lets callers treat it like a 401."""

    status = 401


class JobConflict(JobError):
    """The user already has a running job, or the server is at its job limit."""


class JobNotFound(KeyError):
    pass


def audit_language(audit: Mapping[str, Any] | None) -> str:
    """The audit's PwnDoc locale (``fr``...), ``en`` when unknown."""
    language = audit.get("language") if audit is not None else None
    return language.strip() if isinstance(language, str) and language.strip() else DEFAULT_LOCALE


def id_of(finding: Mapping[str, Any]) -> str:
    return str(finding.get("_id") or finding.get("id") or "")


def _custom_label(entry: Any) -> str:
    custom = entry.get("customField") if isinstance(entry, dict) else None
    if not isinstance(custom, dict):
        return ""
    return str(custom.get("label") or custom.get("display") or "").strip().lower()


def _changed_labels(proposal: Any) -> set[str]:
    return {
        key[len(CUSTOM_PREFIX) :].strip().lower()
        for key in proposal.changes
        if key.startswith(CUSTOM_PREFIX)
    }


def _custom_texts(finding: Mapping[str, Any], labels: set[str]) -> dict[str, Any]:
    return {
        label: entry.get("text")
        for entry in finding.get("customFields") or []
        if (label := _custom_label(entry)) in labels
    }


def _same(a: Any, b: Any) -> bool:
    return (a if a is not None else "") == (b if b is not None else "")


def changed_fields(
    original: Mapping[str, Any], live: Mapping[str, Any], proposal: Any
) -> list[str]:
    """Fields the proposal writes whose live value no longer matches the job-start snapshot."""
    stale = []
    for key in proposal.payload:
        if key == "customFields":
            labels = _changed_labels(proposal) or {_custom_label(e) for e in live.get(key) or []}
            if _custom_texts(original, labels) != _custom_texts(live, labels):
                stale.append(key)
        elif not _same(original.get(key), live.get(key)):
            stale.append(key)
    return stale


def write_payload(live: Mapping[str, Any], proposal: Any) -> dict[str, Any]:
    """The proposal's payload with custom fields merged onto the LIVE list by label."""
    payload = dict(proposal.payload)
    labels = _changed_labels(proposal)
    if "customFields" in payload and labels:
        proposed = {_custom_label(entry): entry.get("text") for entry in payload["customFields"]}
        payload["customFields"] = [
            {**entry, "text": proposed[label]}
            if (label := _custom_label(entry)) in labels and label in proposed
            else entry
            for entry in live.get("customFields") or []
        ]
    return payload


def clean_sources(value: Any) -> tuple[str, ...]:
    """Citation names as a tuple of non-empty strings (a bare string is one citation)."""
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, list | tuple):
        return ()
    return tuple(dict.fromkeys(str(s) for s in value if s))


_PWNDOC_STATUS_TEXT = {403: PWNDOC_DENIED, 404: PWNDOC_MISSING}


def user_message(exc: BaseException) -> str:
    """A fixed, user-safe sentence for ``exc``. The detail belongs in the log, never in a job.

    ``JobError`` texts are written by this package and are safe as they are; everything else
    (PwnDoc, the LLM provider, a bug) maps to a fixed sentence by type and HTTP status only.
    """
    if isinstance(exc, JobError):
        return str(exc)
    status = getattr(exc, "status", None)
    if status == 401:
        return SESSION_EXPIRED
    if isinstance(exc, PwnDocError):
        return _PWNDOC_STATUS_TEXT.get(status, PWNDOC_FAILED)
    if isinstance(exc, LlmAuthError):
        return LLM_KEY_REJECTED
    if isinstance(exc, LlmError):
        return LLM_FAILED
    return UNEXPECTED
