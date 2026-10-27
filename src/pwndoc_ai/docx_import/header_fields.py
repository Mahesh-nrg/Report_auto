"""Audit-level fields of the active template, read by label from the front tables, the page
header, the cover text box and the disclaimer.

The company is a majority vote of four reliable places: the page header (first segment), the
"Name of the Application" table, the cover text box title ("...Report - X", ``mc:Choice`` copy
only) and the disclaimer ("exclusive benefit of X"). Body prose is never a source: the
template's Executive Summary hardcodes another client ("Alfred AI"), which is only reported as
a leftover. The document title and the audit name are never read (the audit name defaults to
the file name in the plan).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from . import labels, limits
from .labels import (
    CONTACT_LABELS,
    cap,
    clean_line,
    first_name_segment,
    is_blank,
    label_key,
    name_key,
    norm,
    severity_of_text,
)
from .model import CompletenessIssue, Contact, HeaderFields, ImportedFinding
from .reader_xml import cell_text, para_text

SOURCES = ("header", "application", "cover", "disclaimer")  # vote tie-break order
COVER = re.compile(r"(?i)\breport\s*[-:|]\s*(?P<name>\S.*)$")
BENEFIT = re.compile(r"(?i)exclusive benefit of\s+(?P<name>.{1,200}?)\s+and is proprietary")
DELIVERED = re.compile(r"(?i)\bdelivered to\s+(?P<name>[^.]{1,200}?)\.(?:\s|$)")
TESTED = re.compile(r"(?i)\btested as of\s+(?P<date>[0-9][0-9./-]{5,12}[0-9])")
DOTTED = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
SLASHED = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
EMAIL_IN = re.compile(r"[^\s@<>\"'(),;:]{1,64}@[A-Za-z0-9.-]{1,189}\.[A-Za-z]{2,24}")
SCOPE_KEYS = frozenset({"scopeurl", "scopeurls", "scope", "inscope", "targetscope", "targets"})
APPLICATION_KEYS = frozenset({"nameoftheapplication", "applicationname", "nameofapplication"})
SEVERITY_KINDS = 3  # a severity table names at least this many severities


@dataclass(frozen=True)
class FrontTables:
    """Front-matter tables as grids (lists of rows of ``w:tc``), by kind."""

    application: tuple = ()
    scope: tuple = ()
    dates: tuple = ()
    severity: tuple = ()
    other: tuple = ()


@dataclass(frozen=True)
class HeaderResult:
    header: HeaderFields
    issues: tuple[CompletenessIssue, ...]
    header_text: str


def _keys(row: list) -> list[str]:
    return [label_key(" ".join(cell_text(cell).split("\n"))) for cell in row]


def classify(grid: list[list]) -> str:
    """``application``, ``scope``, ``dates``, ``severity`` or ``other``, from the header row."""
    if not grid:
        return "other"
    keys = _keys(grid[0])
    if APPLICATION_KEYS & set(keys):
        return "application"
    if "startdate" in keys and "enddate" in keys:
        return "dates"
    if SCOPE_KEYS & set(keys):
        return "scope"
    if sum(1 for k in keys[1:] if severity_of_text(k)) >= SEVERITY_KINDS:
        return "severity"
    return "other"


def _column(grid: Sequence[list], wanted: Iterable[str]) -> list[str]:
    """Texts of the data cells under the first header matching ``wanted``."""
    keys = _keys(grid[0]) if grid else []
    wanted = set(wanted)
    index = next((i for i, key in enumerate(keys) if key in wanted), -1)
    if index < 0:
        return []
    return [cell_text(row[index]) for row in grid[1:] if index < len(row)]


def _name(text: str) -> str:
    value = clean_line(text)
    return "" if is_blank(value) else cap(value, limits.MAX_NAME_CHARS)[0]


# ---------------------------------------------------------------- company


def company_votes(
    header_parts: Sequence, front: FrontTables, textboxes: Sequence[str], prose: Sequence[str]
) -> dict[str, str]:
    """The company name each reliable source gives (sources without one are left out)."""
    found: dict[str, str] = {}
    for root in header_parts:
        lines = [first_name_segment(para_text(p)) for p in root]
        name = _name(next((line for line in lines if line), ""))
        if name:
            found["header"] = name
            break
    for grid in front.application:
        names = [_name(t) for t in _column(grid, APPLICATION_KEYS)]
        if any(names):
            found["application"] = next(n for n in names if n)
            break
    for text in textboxes:
        match = COVER.search(norm(text))
        if match and _name(match.group("name")):
            found["cover"] = _name(match.group("name"))
            break
    for text in prose:
        match = BENEFIT.search(norm(text)) or DELIVERED.search(norm(text))
        if match and _name(match.group("name")):
            found["disclaimer"] = _name(match.group("name"))
            break
    return found


def vote(found: dict[str, str]) -> tuple[str, int, tuple[str, ...]]:
    """(winner, its votes, every distinct name in source order). Ties go to the source listed
    first in ``SOURCES``."""
    distinct: dict[str, str] = {}
    counts: dict[str, int] = {}
    for source in SOURCES:
        if source in found:
            key = name_key(found[source])
            distinct.setdefault(key, found[source])
            counts[key] = counts.get(key, 0) + 1
    if not distinct:
        return "", 0, ()
    best = max(distinct, key=lambda key: counts[key])  # the first maximum wins a tie
    return distinct[best], counts[best], tuple(distinct.values())[: limits.MAX_CANDIDATES]


def leftovers(prose: Sequence[str], company: str) -> tuple[str, ...]:
    """Known template names found in the body text that are not the company itself."""
    found: list[str] = []
    for leftover in sorted(labels.TEMPLATE_LEFTOVER_NAMES):
        if leftover == name_key(company):
            continue
        pattern = re.compile(rf"(?i)(?<![\w]){re.escape(leftover)}(?![\w])")
        for text in prose:
            match = pattern.search(norm(text))
            if match:
                found.append(_name(match.group(0)))
                break
    return tuple(found[: limits.MAX_CANDIDATES])


# ---------------------------------------------------------------- dates, scope, contacts


def _iso(year: int, month: int, day: int) -> str:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def table_date(text: str) -> str:
    """``DD.MM.YYYY`` (the template's de-DE format) or ISO; anything else is blank."""
    value = clean_line(text)
    match = DOTTED.fullmatch(value)
    if match:
        return _iso(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    match = ISO.fullmatch(value)
    return _iso(*(int(g) for g in match.groups())) if match else ""


def disclaimer_date(text: str) -> str:
    """A "tested as of" date, only when unambiguous (``03/06/2026`` is not)."""
    value = text.rstrip(".")
    iso = table_date(value)
    match = SLASHED.fullmatch(value)
    if iso or match is None:
        return iso
    first, second, year = (int(g) for g in match.groups())
    if first > 12 >= second:
        return _iso(year, second, first)
    if second > 12 >= first:
        return _iso(year, first, second)
    return ""


def read_dates(front: FrontTables, prose: Sequence[str]) -> tuple[str, str]:
    start = end = ""
    for grid in front.dates:
        starts = _column(grid, {"startdate"})
        ends = _column(grid, {"enddate"})
        start = start or next((table_date(t) for t in starts if table_date(t)), "")
        end = end or next((table_date(t) for t in ends if table_date(t)), "")
    if not end:
        for text in prose:
            match = TESTED.search(norm(text))
            if match and disclaimer_date(match.group("date")):
                end = disclaimer_date(match.group("date"))
                break
    return start, end


def read_scope(front: FrontTables) -> tuple[tuple[str, ...], bool]:
    found: list[str] = []
    cut = False
    for grid in front.scope:
        for text in _column(grid, SCOPE_KEYS):
            for line in re.split(r"\n|\t| {2,}", text):
                value, was_cut = cap(clean_line(line), limits.MAX_SCOPE_CHARS)
                cut = cut or was_cut
                if value and not is_blank(value) and value not in found:
                    found.append(value)
    if len(found) > limits.MAX_SCOPE_ITEMS:
        return tuple(found[: limits.MAX_SCOPE_ITEMS]), True
    return tuple(found), cut


def _contact(text: str) -> Contact | None:
    value = clean_line(text)
    match = EMAIL_IN.search(value)
    if match is None or not limits.EMAIL.fullmatch(match.group(0)):
        return None
    name = clean_line(re.sub(r"[<>()\[\],;:]", " ", value[: match.start()]))
    parts = [] if is_blank(name) else name.split(" ")
    first = cap(parts[0], limits.MAX_NAME_CHARS)[0] if parts else ""
    last = cap(" ".join(parts[1:]), limits.MAX_NAME_CHARS)[0]
    return Contact(email=match.group(0)[: limits.MAX_EMAIL_CHARS], firstname=first, lastname=last)


def read_contacts(front: FrontTables) -> tuple[Contact, ...]:
    """Contacts from two-cell label rows (``Contact`` / ``Email``) of the front tables."""
    found: dict[str, Contact] = {}
    grids = (*front.application, *front.scope, *front.dates, *front.other)
    for grid in grids:
        for row in grid:
            if len(row) == 2 and label_key(cell_text(row[0])) in CONTACT_LABELS:
                contact = _contact(cell_text(row[1]))
                if contact is not None:
                    found.setdefault(contact.email.casefold(), contact)
    return tuple(found.values())[: limits.MAX_CANDIDATES]


def read_test_type(front: FrontTables) -> str:
    for grid in front.application:
        names = [_name(t) for t in _column(grid, {"testtype", "typeoftest", "testingtype"})]
        if any(names):
            return next(n for n in names if n)
    return ""


# ---------------------------------------------------------------- severity table


def severity_table(front: FrontTables) -> tuple[tuple[str, int], ...]:
    """``(severity, count)`` as printed; blank cells are left out."""
    for grid in front.severity:
        if len(grid) < 2:
            continue
        found: list[tuple[str, int]] = []
        for head, value in zip(grid[0][1:], grid[1][1:], strict=False):
            name = severity_of_text(cell_text(head))
            text = clean_line(cell_text(value))
            if name and text.isdigit() and len(text) < 6 and name not in dict(found):
                found.append((name, int(text)))
        return tuple(found)
    return ()


def severity_mismatch(
    table: Sequence[tuple[str, int]], findings: Sequence[ImportedFinding]
) -> bool:
    counts: dict[str, int] = {}
    for finding in findings:
        if finding.include and finding.severity:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return any(counts.get(name, 0) != count for name, count in table)


# ---------------------------------------------------------------- assembly


def _table_lines(grids: Iterable) -> list[str]:
    lines: list[str] = []
    for grid in grids:
        for row in grid:
            cells = [clean_line(cell_text(cell)) for cell in row]
            if any(cells):
                lines.append(" | ".join(cells))
    return lines


def excerpt(
    header_parts: Sequence, front: FrontTables, textboxes: Sequence[str], prose: Sequence[str]
) -> str:
    """``header_text``: page header, cover, front tables and disclaimer sentences, capped."""
    lines = [clean_line(para_text(p)) for root in header_parts for p in root]
    lines += [clean_line(t) for t in textboxes]
    lines += _table_lines((*front.application, *front.scope, *front.dates, *front.severity))
    lines += [clean_line(t) for t in prose if BENEFIT.search(norm(t)) or TESTED.search(norm(t))]
    text = "\n".join(line for line in lines if line)
    return cap(text, limits.MAX_HEADER_TEXT_CHARS)[0]


def read_header(
    header_parts: Sequence, front: FrontTables, textboxes: Sequence[str], prose: Sequence[str]
) -> HeaderResult:
    """Every audit-level field and the reader's header issues. ``header_parts`` holds, per page
    header part, its paragraphs (text boxes excluded)."""
    company, votes, candidates = vote(company_votes(header_parts, front, textboxes, prose))
    start, end = read_dates(front, prose)
    scope, scope_cut = read_scope(front)
    left = leftovers(prose, company)
    contacts = read_contacts(front)
    header = HeaderFields(
        company=company,
        company_votes=votes,
        company_candidates=candidates,
        scope=scope,
        date_start=start,
        date_end=end,
        test_type=read_test_type(front),
        leftover_names=left,
        contacts=contacts,
        severity_table=severity_table(front),
    )
    issues = [
        CompletenessIssue("leftover_name") if left else None,
        CompletenessIssue("company_conflict") if len(candidates) > 1 else None,
        CompletenessIssue("contacts_absent") if not contacts else None,
        CompletenessIssue("text_truncated", fields=("scope",)) if scope_cut else None,
    ]
    text = excerpt(header_parts, front, textboxes, prose)
    return HeaderResult(header, tuple(i for i in issues if i is not None), text)
