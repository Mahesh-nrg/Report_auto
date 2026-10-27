"""The calendar dates a text prints, for grounding an LLM's date answer.

A date answer is accepted only when it equals a date written in the header excerpt: ISO
``2026-03-05`` (or with ``/`` or ``.``), ``05.03.2026`` / ``05/03/2026`` / ``05-03-2026`` (both
the day-first and the month-first reading, since the excerpt does not say which), and spelled
months: ``5 March 2026``, ``5th of March, 2026``, ``March 5, 2026``, ``Mar 5 2026``. Numbers that
merely appear somewhere (a version, a page number) never make a date.
"""

from __future__ import annotations

import re
from datetime import date

MONTHS = {
    name: number
    for number, names in enumerate(
        (("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
         ("may",), ("june", "jun"), ("july", "jul"), ("august", "aug"),
         ("september", "sep", "sept"), ("october", "oct"), ("november", "nov"),
         ("december", "dec")),
        start=1,
    )
    for name in names
}  # fmt: skip
_SUFFIX = r"(?:st|nd|rd|th)?"
ISO = re.compile(r"(?<!\d)(\d{4})([-/.])(\d{1,2})\2(\d{1,2})(?!\d)")
NUMERIC = re.compile(r"(?<!\d)(\d{1,2})([-/.])(\d{1,2})\2(\d{4})(?!\d)")
DAY_FIRST = re.compile(
    rf"(?<![\d\w])(\d{{1,2}}){_SUFFIX}\s+(?:of\s+)?([A-Za-z]{{3,9}})\.?,?\s+(\d{{4}})(?!\d)"
)
MONTH_FIRST = re.compile(rf"(?<!\w)([A-Za-z]{{3,9}})\.?\s+(\d{{1,2}}){_SUFFIX},?\s+(\d{{4}})(?!\d)")


def _date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _numeric(text: str) -> list[date | None]:
    found = [_date(int(y), int(m), int(d)) for y, _, m, d in ISO.findall(text)]
    for first, _, second, year in NUMERIC.findall(text):
        found += [
            _date(int(year), int(second), int(first)),
            _date(int(year), int(first), int(second)),
        ]
    return found


def _spelled(text: str) -> list[date | None]:
    found = []
    for day, month, year in DAY_FIRST.findall(text):
        number = MONTHS.get(month.casefold())
        found.append(_date(int(year), number, int(day)) if number else None)
    for month, day, year in MONTH_FIRST.findall(text):
        number = MONTHS.get(month.casefold())
        found.append(_date(int(year), number, int(day)) if number else None)
    return found


def dates_in(text: str) -> frozenset[date]:
    """Every valid calendar date ``text`` writes out."""
    return frozenset(d for d in (*_numeric(text), *_spelled(text)) if d is not None)


__all__ = ["dates_in"]
