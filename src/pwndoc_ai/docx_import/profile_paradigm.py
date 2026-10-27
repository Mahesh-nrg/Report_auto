"""Audit-level fields of the ParadigmIT VAPT layout (the ``paradigm`` profile).

The layout prints its audit fields as label-over-value cards: one cell holds the label line
("PREPARED FOR", "ASSESSMENT WINDOW", "TARGET APPLICATION", "APPLICATION URL", "TEST TYPE",
"CLIENT", "IDENTIFIED FINDINGS") and the value line under it. The vendor name in the page
header ("PARADIGMIT CYBERSECURITY / VAPT") and the disclaimer's generic "the Client" are never
the company: the company is the "PREPARED FOR" card, confirmed by the "CLIENT" card.

The executive numbers (the "Finding distribution" chips and "IDENTIFIED FINDINGS") are only
consistency checks: they fill ``severity_table`` (the existing ``severity_table_mismatch``
check) and ``printed_total`` (checked by the reader against the findings found).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from . import header_fields as hf
from . import limits
from .labels import cap, clean_line, is_blank, label_key, name_key, severity_of_text
from .model import CompletenessIssue, HeaderFields
from .reader_xml import cell_text
from .textdates import MONTHS, dates_in

COMPANY_KEYS = ("preparedfor", "client", "clientname")
APP_KEYS = ("targetapplication", "application", "applicationname")
URL_KEYS = ("applicationurl", "targeturl", "url", "scope", "inscope")
TYPE_KEYS = ("testtype", "assessment", "assessmenttype")
WINDOW_KEYS = ("assessmentwindow", "testingwindow", "assessmentperiod", "testwindow")
TOTAL_KEYS = ("identifiedfindings", "totalfindings")
GENERIC_CLIENT = frozenset({"theclient", "client"})
WINDOW = re.compile(
    r"(?i)(?P<d1>\d{1,2})\s+(?P<m1>[a-z]{3,9})\.?(?:\s+(?P<y1>\d{4}))?\s*"
    r"(?:-|to|until)\s*(?P<d2>\d{1,2})\s+(?P<m2>[a-z]{3,9})\.?,?\s+(?P<y2>\d{4})"
)
NUMERIC_WINDOW = re.compile(
    r"(?P<d1>\d{1,2})[/.-](?P<m1>\d{1,2})[/.-](?P<y1>\d{4})\s*(?:-|\u2013|\u2014|to|until)\s*"
    r"(?P<d2>\d{1,2})[/.-](?P<m2>\d{1,2})[/.-](?P<y2>\d{4})",
    re.IGNORECASE,
)
MAX_COUNT_DIGITS = 5
CHIP_KEYS = frozenset({"critical", "high", "medium", "low", "info", "informational"})


@dataclass(frozen=True)
class Cards:
    """Label-over-value pairs (first value per label key) and the severity chips."""

    values: dict[str, str]
    chips: tuple[tuple[str, int], ...]


def _lines(tc) -> list[str]:
    return [clean_line(t) for t in cell_text(tc).split("\n") if clean_line(t)]


def _grids(front: hf.FrontTables) -> Iterable[list]:
    for kind in ("application", "scope", "dates", "severity", "other"):
        yield from getattr(front, kind)


def read_cards(front: hf.FrontTables) -> Cards:
    values: dict[str, str] = {}
    chips: dict[str, int] = {}
    for grid in _grids(front):
        for row in grid:
            for tc in row:
                lines = _lines(tc)
                if len(lines) < 2:
                    continue
                key, value = label_key(lines[0]), lines[1]
                if key in CHIP_KEYS:  # a severity chip: "Critical" over "02"
                    if value.isdigit() and len(value) <= MAX_COUNT_DIGITS:
                        chips.setdefault(severity_of_text(key), int(value))
                    continue
                if not is_blank(value):
                    values.setdefault(key, value)
    return Cards(values, tuple(chips.items()))


def _first(cards: Cards, keys: Sequence[str]) -> str:
    return next((cards.values[k] for k in keys if k in cards.values), "")


def _date(year: int, month_name: str, day: int) -> date | None:
    month = MONTHS.get(month_name.casefold().rstrip("."))
    try:
        return date(year, month, day) if month else None
    except ValueError:
        return None


def _numeric_window(text: str) -> tuple[str, str]:
    """Day-first ``31/08/2026 - 08/09/2026`` (the PwnDoc template's own date format)."""
    match = NUMERIC_WINDOW.search(text)
    if match is None:
        return "", ""
    try:
        start = date(int(match["y1"]), int(match["m1"]), int(match["d1"]))
        end = date(int(match["y2"]), int(match["m2"]), int(match["d2"]))
    except ValueError:
        return "", ""
    return (start.isoformat(), end.isoformat()) if start <= end else ("", "")


def parse_window(text: str) -> tuple[str, str]:
    """(start, end) ISO dates of ``31 AUG - 08 SEP 2026`` / ``31 Aug 2026 - 08 Sep 2026`` /
    ``31/08/2026 - 08/09/2026`` or of two written dates; ``("", "")`` when unclear."""
    numeric = _numeric_window(clean_line(text))
    if numeric[0]:
        return numeric
    match = WINDOW.search(clean_line(text))
    if match is not None:
        end = _date(int(match["y2"]), match["m2"], int(match["d2"]))
        start_year = int(match["y1"] or match["y2"])
        start = _date(start_year, match["m1"], int(match["d1"]))
        if start and end and start > end and not match["y1"]:
            start = _date(start_year - 1, match["m1"], int(match["d1"]))
        if start and end and start <= end:
            return start.isoformat(), end.isoformat()
    found = sorted(dates_in(text))
    if len(found) == 2:  # noqa: PLR2004 - a window is two dates
        return found[0].isoformat(), found[1].isoformat()
    return "", ""


def _company(cards: Cards) -> tuple[str, tuple[str, ...]]:
    names: list[str] = []
    for key in COMPANY_KEYS:
        value = cards.values.get(key, "")
        if value and name_key(value).replace(" ", "") not in GENERIC_CLIENT:
            names.append(cap(value, limits.MAX_NAME_CHARS)[0])
    distinct = list(dict.fromkeys(names))
    return (distinct[0] if distinct else ""), tuple(distinct)


def _votes(cards: Cards, company: str) -> int:
    if not company:
        return 0
    return sum(1 for k in COMPANY_KEYS if name_key(cards.values.get(k, "")) == name_key(company))


def printed_total(front: hf.FrontTables) -> int | None:
    """The "IDENTIFIED FINDINGS" number, when printed."""
    value = _first(read_cards(front), TOTAL_KEYS)
    return int(value) if value.isdigit() and len(value) <= MAX_COUNT_DIGITS else None


def read_header(
    header_parts: Sequence, front: hf.FrontTables, textboxes: Sequence[str], prose: Sequence[str]
) -> hf.HeaderResult:
    """``header_fields.read_header`` for the Paradigm layout."""
    cards = read_cards(front)
    company, candidates = _company(cards)
    start, end = parse_window(_first(cards, WINDOW_KEYS))
    url = _first(cards, URL_KEYS)
    contacts = hf.read_contacts(front)
    header = HeaderFields(
        company=company,
        company_votes=_votes(cards, company),
        company_candidates=candidates,
        scope=(url,) if url else (),
        date_start=start,
        date_end=end,
        test_type=_first(cards, TYPE_KEYS),
        contacts=contacts,
        severity_table=cards.chips,
    )
    issues = [
        CompletenessIssue("company_conflict") if len(candidates) > 1 else None,
        CompletenessIssue("contacts_absent") if not contacts else None,
    ]
    lines = [f"{k}: {v}" for k, v in cards.values.items()]
    text = "\n".join([*lines, *hf.excerpt(header_parts, front, textboxes, prose).split("\n")])
    excerpt = cap(text, limits.MAX_HEADER_TEXT_CHARS)[0]
    return hf.HeaderResult(header, tuple(i for i in issues if i), excerpt)


__all__ = ["parse_window", "printed_total", "read_cards", "read_header"]
