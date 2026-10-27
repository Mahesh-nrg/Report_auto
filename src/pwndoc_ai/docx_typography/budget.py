"""One wall-clock budget per typography call (same design as ``docx_layout.budget``).

Nested ``limit()`` blocks keep the earlier deadline. Hot loops call ``check()``; once the budget is
spent the call stops with ``TypographyError("too_complex")`` and the caller keeps the input bytes.
"""

from __future__ import annotations

import contextvars
import time
from collections.abc import Iterator
from contextlib import contextmanager

from .errors import TypographyError
from .limits import MAX_SECONDS

_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "typography_deadline", default=None
)


@contextmanager
def limit(seconds: float | None = None) -> Iterator[None]:
    """Run the block under a budget of ``seconds`` (default ``MAX_SECONDS``)."""
    new = time.monotonic() + (MAX_SECONDS if seconds is None else max(0.0, float(seconds)))
    current = _deadline.get()
    token = _deadline.set(new if current is None else min(current, new))
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining() -> float | None:
    deadline = _deadline.get()
    return None if deadline is None else deadline - time.monotonic()


def check() -> None:
    """Raise ``TypographyError("too_complex")`` when the current budget is spent."""
    left = remaining()
    if left is not None and left <= 0:
        raise TypographyError("too_complex")
