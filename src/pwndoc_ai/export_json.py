"""Strict validators for untrusted JSON of the Export tab (browser bodies and files on disk).

Each helper returns the checked value or raises :class:`ExportInputError` naming the schema path.
Paths are built from the contract's own key names and indices, never from user text. Pure
Python: no lxml, no third-party import, so config.py and the routes may import it cheaply.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from .export_errors import ExportInputError

T = TypeVar("T")

CONTROL_LINE = re.compile(r"[\x00-\x1f\x7f-\x9f  ￾￿]")
CONTROL_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f￾￿]")
SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_COUNT = 10_000_000


def fail(path: str, code: str | None = None) -> ExportInputError:
    return ExportInputError(code, field=path)


def record(value: Any, path: str, needed: Iterable[str], allowed_extra: Iterable[str] = ()) -> dict:
    """``value`` as a dict with exactly the allowed keys, every needed key present."""
    need = set(needed)
    allowed = need | set(allowed_extra)
    if not isinstance(value, dict) or not need.issubset(value):
        raise fail(path)
    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise fail(path)
    return value


def mapping(value: Any, path: str, limit: int, key: re.Pattern) -> dict[str, Any]:
    """A dict of at most ``limit`` entries whose keys fully match ``key``."""
    if not isinstance(value, dict) or len(value) > limit:
        raise fail(path)
    if any(not isinstance(name, str) or not key.fullmatch(name) for name in value):
        raise fail(path)
    return value


def text(
    value: Any, path: str, limit: int, *, line: bool = True, pattern: re.Pattern | None = None
) -> str:
    """A string up to ``limit`` characters. ``line`` refuses every control character, otherwise
    TAB, LF and CR are allowed. An empty string always passes; ``pattern`` must fully match a
    non-empty one."""
    if not isinstance(value, str) or len(value) > limit:
        raise fail(path)
    if not value:
        return ""
    if (CONTROL_LINE if line else CONTROL_TEXT).search(value):
        raise fail(path)
    if pattern is not None and not pattern.fullmatch(value):
        raise fail(path)
    return value


def required(value: Any, path: str, limit: int, pattern: re.Pattern) -> str:
    """A non-empty string matching ``pattern``."""
    checked = text(value, path, limit, pattern=pattern)
    if not checked:
        raise fail(path)
    return checked


def flag(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise fail(path)
    return value


def count(value: Any, path: str, limit: int = MAX_COUNT) -> int:
    """A non-negative int (bool refused) up to ``limit``."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= limit:
        raise fail(path)
    return value


def number(value: Any, path: str, low: float, high: float) -> float:
    """A finite int or float (bool refused) within ``[low, high]``, returned as float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise fail(path)
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise fail(path)
    return result


def choice(value: Any, path: str, choices: Iterable[str]) -> str:
    if not isinstance(value, str) or value not in set(choices):
        raise fail(path)
    return value


def items(value: Any, path: str, limit: int, parse: Callable[[Any, str], T]) -> tuple[T, ...]:
    """A list of at most ``limit`` entries, each parsed with ``parse(entry, "path[i]")``."""
    if not isinstance(value, list) or len(value) > limit:
        raise fail(path)
    return tuple(parse(entry, f"{path}[{index}]") for index, entry in enumerate(value))


def strings(
    value: Any, path: str, limit: int, chars: int, pattern: re.Pattern | None = None
) -> tuple[str, ...]:
    """A list of non-empty one-line strings."""
    return items(value, path, limit, lambda entry, at: _non_empty(entry, at, chars, pattern))


def _non_empty(value: Any, path: str, chars: int, pattern: re.Pattern | None) -> str:
    checked = text(value, path, chars, pattern=pattern)
    if not checked:
        raise fail(path)
    return checked


def unique(keys: Iterable[str], path: str) -> None:
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            raise fail(path)
        seen.add(key)


__all__ = [
    "CONTROL_LINE",
    "CONTROL_TEXT",
    "MAX_COUNT",
    "SHA256",
    "choice",
    "count",
    "fail",
    "flag",
    "items",
    "mapping",
    "number",
    "record",
    "required",
    "strings",
    "text",
    "unique",
]
