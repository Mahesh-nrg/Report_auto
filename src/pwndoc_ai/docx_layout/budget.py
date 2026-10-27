"""One wall-clock budget for a layout call (inspect or tidy).

A hostile document inside the size caps can still make a quadratic loop run for minutes. The entry
points open ``limit()`` and the hot loops call ``check()``: once the budget is spent the call stops
with a ``DocxLayoutError`` (the file is then served as it is). Nested ``limit()`` blocks keep the
earlier deadline, so ``tidy_docx`` calling ``inspect_layout`` does not get a second budget.
"""

from __future__ import annotations

import contextvars
import time
from collections.abc import Iterator
from contextlib import contextmanager

from .errors import DocxLayoutError

MAX_SECONDS = 20.0  # for one inspect or tidy call
TOO_COMPLEX = "The document is too large or complex."

_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "layout_deadline", default=None
)


@contextmanager
def limit(seconds: float | None = None) -> Iterator[None]:
    """Run the block under a budget of ``seconds`` (default ``MAX_SECONDS``)."""
    new = time.monotonic() + (MAX_SECONDS if seconds is None else seconds)
    current = _deadline.get()
    token = _deadline.set(new if current is None else min(current, new))
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining() -> float | None:
    """Seconds left in the current budget, None when no budget is open."""
    deadline = _deadline.get()
    return None if deadline is None else deadline - time.monotonic()


def check() -> None:
    """Raise ``DocxLayoutError`` when the current budget is spent."""
    left = remaining()
    if left is not None and left <= 0:
        raise DocxLayoutError(TOO_COMPLEX)
