"""Reading and writing findings JSON files (any common encoding in, UTF-8 out)."""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

FALLBACK_ENCODINGS = ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "cp1252")


class JsonFileError(RuntimeError):
    """Raised when a findings file cannot be read or has the wrong shape."""


def _decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for encoding in FALLBACK_ENCODINGS:
        try:
            text = raw.decode(encoding)
            json.loads(text)
            return text
        except (UnicodeDecodeError, ValueError):
            continue
    raise JsonFileError("file is not valid JSON in any supported encoding")


def read_findings(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Load a list of finding dicts. An empty file is an empty list."""
    file = Path(path)
    if not file.is_file():
        raise JsonFileError(f"{file} not found")
    raw = file.read_bytes()
    if not raw.strip():
        return []
    data = json.loads(_decode(raw))
    if isinstance(data, dict):
        # tolerate a full audit export or an {id: finding} mapping
        data = (
            data.get("findings") if isinstance(data.get("findings"), list) else list(data.values())
        )
    if not isinstance(data, list):
        raise JsonFileError("expected a JSON list of findings")
    return [item for item in data if isinstance(item, dict)]


def read_findings_or_empty(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    try:
        return read_findings(path)
    except (JsonFileError, ValueError):
        return []


def _make_private_dirs(directory: Path) -> None:
    """Create missing parents with mode 0700 (findings are client-confidential)."""
    missing = [d for d in (directory, *directory.parents) if not d.exists()]
    for folder in reversed(missing):
        folder.mkdir(mode=0o700, exist_ok=True)


def write_json_atomic(path: str | os.PathLike[str], data: Any) -> None:
    """Write UTF-8 JSON (mode 0600) via a temp file so an interrupted run never corrupts it."""
    target = Path(path)
    _make_private_dirs(target.parent)
    tmp = target.with_name(target.name + ".tmp")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    os.replace(tmp, target)


def finding_key(finding: dict[str, Any]) -> str:
    """Stable identity for resume/skip logic: _id, then id, then title."""
    for name in ("_id", "id"):
        if finding.get(name) is not None:
            return str(finding[name])
    title = str(finding.get("title", "")).strip()
    return title or json.dumps(finding, sort_keys=True)


def finding_keys(findings: list[dict[str, Any]]) -> list[str]:
    """Keys for a whole list; repeated title-only keys get ``#n`` so they never collide."""
    keys = [finding_key(f) for f in findings]
    has_id = [f.get("_id") is not None or f.get("id") is not None for f in findings]
    totals = Counter(k for k, ident in zip(keys, has_id, strict=True) if not ident)
    seen: Counter[str] = Counter()
    unique: list[str] = []
    for key, ident in zip(keys, has_id, strict=True):
        if ident or totals[key] < 2:
            unique.append(key)
            continue
        seen[key] += 1
        unique.append(f"{key}#{seen[key]}")
    return unique
