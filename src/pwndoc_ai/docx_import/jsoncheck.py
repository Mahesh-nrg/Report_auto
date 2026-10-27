"""Strict validators for untrusted JSON (browser bodies and files on disk).

Each helper returns the checked value or raises :class:`ImportInputError` naming the schema
path. Paths are built from the importer's own key names and indices, never from user text.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import date
from typing import Any, TypeVar

from .errors import ImportInputError
from .limits import (
    CONTROL_LINE,
    CONTROL_TEXT,
    ISO_DATE,
    MAX_COUNT,
)

T = TypeVar("T")


def fail(path: str) -> ImportInputError:
    return ImportInputError(field=path)


def record(value: Any, path: str, needed: Iterable[str], allowed_extra: Iterable[str] = ()) -> dict:
    """``value`` as a dict with exactly the allowed keys, every needed key present."""
    need = set(needed)
    allowed = need | set(allowed_extra)
    if not isinstance(value, dict) or not need.issubset(value):
        raise fail(path)
    if any(not isinstance(key, str) or key not in allowed for key in value):
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


def flag(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise fail(path)
    return value


def count(value: Any, path: str, limit: int = MAX_COUNT) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= limit:
        raise fail(path)
    return value


def choice(value: Any, path: str, choices: Iterable[str]) -> str:
    if not isinstance(value, str) or value not in tuple(choices):
        raise fail(path)
    return value


def items(value: Any, path: str, limit: int, parse: Callable[[Any, str], T]) -> tuple[T, ...]:
    if not isinstance(value, list) or len(value) > limit:
        raise fail(path)
    return tuple(parse(item, f"{path}[{index}]") for index, item in enumerate(value))


def lines(value: Any, path: str, limit: int, chars: int) -> tuple[str, ...]:
    """A list of non-empty one-line strings."""
    parsed = items(value, path, limit, lambda item, where: text(item, where, chars))
    if any(not item for item in parsed):
        raise fail(path)
    return parsed


def flags(value: Any, path: str, allowed: frozenset[str]) -> tuple[str, ...]:
    parsed = items(value, path, len(allowed), lambda item, where: choice(item, where, allowed))
    if len(set(parsed)) != len(parsed):
        raise fail(path)
    return parsed


def iso_date(value: Any, path: str) -> str:
    found = text(value, path, 10, pattern=ISO_DATE)
    if found:
        try:
            date.fromisoformat(found)
        except ValueError:
            raise fail(path) from None
    return found


def unique(keys: Iterable[str], path: str) -> None:
    seen = list(keys)
    if len(seen) != len(set(seen)):
        raise fail(path)


def optional(value: Any, parse: Callable[[Any, str], T], path: str) -> T | None:
    return None if value is None else parse(value, path)


def required(value: Any, path: str, limit: int, pattern: re.Pattern) -> str:
    found = text(value, path, limit, pattern=pattern)
    if not found:
        raise fail(path)
    return found
