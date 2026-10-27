"""Findings of the active template: the detail tables and the summary table, read by label.

A detail table has one row per field, the label in the first cell(s) and the value in the last
(the "Vulnerable Point / Error Location" group has a sub-label cell in between, and the
"Proof of concept:" row is a single cell that starts with its label). Labels are matched through
``labels.FIELD_LABELS`` so wording changes and synonyms still map. The summary table ("List of
Vulnerable Parameter, Location discovered") has one row per finding with the URL, parameter and
location in one labelled cell.

Values are kept as found: nothing is invented. CVSS is taken only when printed (a score or a
valid ``CVSS:3.x`` vector); severities map only from a known word.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from .. import cvss
from . import limits, profile
from .captions import strip_caption
from .html import Para, join_capped, number_lists, render
from .labels import (
    POC_INLINE_LABELS,
    cap,
    clean_line,
    field_of,
    is_blank,
    label_key,
    severity_of_text,
)
from .reader_xml import Counter, ImageRef, cell_items, cell_text, rows, w, wattr

SPLIT_URLS = re.compile(r"\n|\t| {2,}")
SUMMARY_LABELS = re.compile(r"(?im)^\s*(urls?|parameters?|location|path)\s*:\s*")
SUMMARY_LABELS_PARAM = re.compile(r"(?im)^\s*(urls?|parameters?|params?|location|path)\s*:\s*")
URL_START = re.compile(r"(?i)^(?:[a-z][a-z0-9+.-]{1,15}://|www\.|/|\S+\.[a-z]{2,24}(?:[/:]|$))")
FREE_CAPTION_CHARS = 160
POC_LABEL = re.compile(r"(?is)^\s*(?:proof\s+of\s+concept|poc)\b\s*[:.-]?\s*")
VECTOR = re.compile(r"CVSS:3\.[01]/[A-Za-z:/]{3,200}")
SCORE = re.compile(r"(?<![\d.])(10(?:\.0+)?|\d(?:\.\d+)?)(?![\d.])")
SOURCE_DIGITS = re.compile(r"\d{1,6}")
BULLET = re.compile(r"^\s*(?:[-*\u2022\u00b7]|\d{1,3}[.)])\s+")
LINE_FIELDS = frozenset(
    {"id", "title", "location", "parameter", "cwe", "urls", "severity", "cvss", "references"}
)  # one-line cells; every other field is rich text (description, observation, remediation)
ID_KEYS = frozenset({"sno", "no", "srno", "serialno", "id", "slno", "findingid"})


@dataclass(frozen=True)
class Figure:
    """A picture with its caption (``captioned`` is False when no caption paragraph names it)."""

    rid: str
    order: int
    caption: str = ""
    captioned: bool = False
    unmatched: bool = False


@dataclass(frozen=True)
class RawFinding:
    """One copy of a finding as read (a detail table or a summary row), before dedupe."""

    position: int
    origin: str  # "detail" or "summary"
    source_id: str = ""
    title: str = ""
    severity: str = ""
    cvss_vector: str = ""
    cvss_score: str = ""
    cwe: str = ""
    urls: tuple[str, ...] = ()
    parameter: str = ""
    location: str = ""
    description: str = ""
    observation: str = ""
    remediation: str = ""
    references: tuple[str, ...] = ()
    poc: tuple = ()  # Para and Figure items, in order
    figures: tuple[Figure, ...] = ()  # pictures from other cells or after the table
    truncated: tuple[str, ...] = ()  # names of fields that were cut to their cap


# ---------------------------------------------------------------- values


def one_line(text: str, limit: int) -> tuple[str, bool]:
    value = clean_line(text)
    return ("", False) if is_blank(value) else cap(value, limit)


def _url_lines(text: str) -> list[str]:
    """The URL cell's lines; with the profile's ``join_wrapped_urls`` a line that does not start
    a URL (``data={...}`` after ``...request?``) continues the line before it."""
    parts = SPLIT_URLS.split(text)
    if not profile.active().join_wrapped_urls:
        return parts
    out: list[str] = []
    for part in (p.strip() for p in parts):
        if out and part and not URL_START.match(part) and not is_blank(part):
            out[-1] += part
        else:
            out.append(part)
    return out


def split_urls(text: str) -> tuple[tuple[str, ...], bool]:
    found: list[str] = []
    cut = False
    for part in _url_lines(text):
        value, was_cut = one_line(part, limits.MAX_LINE_CHARS)
        cut = cut or was_cut
        if value and value not in found:
            found.append(value)
    if len(found) > limits.MAX_URLS:
        return tuple(found[: limits.MAX_URLS]), True
    return tuple(found), cut


def parse_cvss(text: str) -> tuple[str, str]:
    """(vector, score) exactly as printed; a vector only when it parses, a score only when it
    is a plain 0-10 number with at most one decimal."""
    value = clean_line(text)
    vector = ""
    match = VECTOR.search(value)
    if match is not None:
        parsed = cvss.parse_vector(match.group(0))
        if parsed is not None and limits.CVSS_VECTOR.fullmatch(parsed.vector):
            vector = parsed.vector
        value = value.replace(match.group(0), " ")
    score = ""
    found = SCORE.search(value)
    if found is not None:
        raw = found.group(1)
        trimmed = raw.rstrip("0").rstrip(".") if "." in raw else raw
        if trimmed == "10" and raw.startswith("10."):
            trimmed = "10.0"
        if limits.CVSS_SCORE.fullmatch(trimmed):
            score = trimmed
    return vector, score


def source_id(text: str) -> str:
    match = SOURCE_DIGITS.search(clean_line(text))
    return match.group(0) if match is not None else ""


def references(text: str) -> tuple[tuple[str, ...], bool]:
    found: list[str] = []
    cut = False
    for line in text.split("\n"):
        value, was_cut = one_line(BULLET.sub("", line), limits.MAX_REFERENCE_CHARS)
        cut = cut or was_cut
        if value and value not in found:
            found.append(value)
    if len(found) > limits.MAX_REFERENCES:
        return tuple(found[: limits.MAX_REFERENCES]), True
    return tuple(found), cut


# ---------------------------------------------------------------- figures and captions


def pair_figures(items: list, *, unmatched: bool = False) -> list:
    """Pictures become ``Figure`` items holding the caption paragraph that follows them (or, for
    a picture with none, a caption paragraph directly before it). Used captions are removed."""
    out: list = []
    for item in items:
        if isinstance(item, ImageRef):
            out.append(Figure(item.rid, item.order, unmatched=unmatched))
            continue
        index = _last_non_blank(out)
        caption = None if item.list_kind else _caption_after(item.text, index >= 0)
        if caption is not None and index >= 0 and _open_figure(out[index]):
            out[index] = replace(out[index], caption=caption, captioned=True)
            continue
        out.append(item)
    return _captions_above(out)


def _caption_after(text: str, after_item: bool) -> str | None:
    """A caption for the picture just before ``text``: a ``Fig``/``Figure N`` line, or with the
    profile's ``free_captions`` any short single line that does not end with a colon."""
    caption = strip_caption(text)
    if caption is not None or not (after_item and profile.active().free_captions):
        return caption
    value = clean_line(text)
    if not value or "\n" in text.strip() or len(value) > FREE_CAPTION_CHARS:
        return None
    if value.endswith(":") or is_blank(value) or BULLET.match(value):
        return None
    return value


def _open_figure(item) -> bool:
    return isinstance(item, Figure) and not item.captioned


def _last_non_blank(items: list) -> int:
    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        if not (isinstance(item, Para) and not item.text.strip()):
            return index
    return -1


def _captions_above(items: list) -> list:
    out: list = []
    for item in items:
        if _open_figure(item):
            index = _last_non_blank(out)
            before = out[index] if index >= 0 else None
            caption = strip_caption(before.text) if isinstance(before, Para) else None
            if caption is not None and not before.list_kind:
                del out[index]
                item = replace(item, caption=caption, captioned=True)
        out.append(item)
    return out


def split_figures(items: list) -> tuple[list[Para], list[Figure]]:
    paras = [item for item in items if isinstance(item, Para)]
    return paras, [item for item in items if isinstance(item, Figure)]


# ---------------------------------------------------------------- detail tables


def row_label(cells: list) -> tuple[str, object]:
    """(field, value cell) of one detail-table row; ``("poc", cell)`` for the single-cell PoC
    row; ``("", None)`` for an unknown row. The most specific (rightmost) label wins."""
    if len(cells) == 1:
        first = next((line for line in cell_text(cells[0]).split("\n") if line.strip()), "")
        key = label_key(POC_LABEL.match(first).group(0)) if POC_LABEL.match(first) else ""
        return ("poc", cells[0]) if key in POC_INLINE_LABELS else ("", None)
    for cell in reversed(cells[:-1]):
        field = field_of(" ".join(cell_text(cell).split("\n")))
        if field and field != "group":
            return field, cells[-1]
    return "", None


def is_detail_table(grid: list[list]) -> bool:
    fields = {row_label(cells)[0] for cells in grid[:40] if 1 <= len(cells) <= 4}
    known = fields - {"", "id"}
    return "title" in known and len(known) >= 3


def _strip_poc_label(items: list) -> list:
    for index, item in enumerate(items):
        if isinstance(item, Para) and item.text.strip():
            rest = POC_LABEL.sub("", item.text, count=1)
            head = [] if not rest.strip() else [replace(item, text=rest)]
            return items[:index] + head + items[index + 1 :]
    return items


@dataclass(frozen=True)
class _Cell:
    field: str
    text: str
    items: tuple


def _detail_cells(grid: list[list], numbering: dict, counter: Counter) -> tuple[list[_Cell], str]:
    cells: list[_Cell] = []
    first_id = ""
    for index, row in enumerate(grid):
        field, value = row_label(row)
        if index == 0 and not field and len(row) >= 2:
            if label_key(cell_text(row[0])) in ID_KEYS:
                first_id = source_id(cell_text(row[-1]))
            continue
        if value is None:
            _continue_cell(cells, row, numbering, counter)
            continue
        items = number_lists(cell_items(value, numbering, counter))
        text = "\n".join(i.text for i in items if isinstance(i, Para))
        if field == "severity" and not severity_of_text(text):
            text = severity_fill(value) or text
        cells.append(_Cell(field, text, tuple(items)))
    return cells, first_id


def _continue_cell(cells: list[_Cell], row: list, numbering: dict, counter: Counter) -> None:
    """With the profile's ``continuation_rows`` an unlabelled full-width row continues the
    field above it (a label row "Proof of Concept" over a row with the evidence)."""
    if not (cells and len(row) == 1 and profile.active().continuation_rows):
        return
    extra = number_lists(cell_items(row[0], numbering, counter))
    last = cells[-1]
    text = "\n".join(i.text for i in extra if isinstance(i, Para))
    joined = "\n".join(t for t in (last.text, text) if t)
    cells[-1] = replace(last, text=joined, items=last.items + tuple(extra))


def severity_fill(tc) -> str:
    """The severity a cell's shading colour stands for, by the profile's chip palette."""
    fills = profile.active().severity_fills
    tcpr = tc.find(w("tcPr")) if fills else None
    shd = tcpr.find(w("shd")) if tcpr is not None else None
    fill = (wattr(shd, "fill") or "") if shd is not None else ""
    return fills.get(fill.upper(), "")


def _with_steps(cells: list[_Cell]) -> list[_Cell]:
    """The profile's "Steps to Reproduce" row joined to the PoC (after the evidence), led by
    ``steps_label``; PwnDoc has no steps field."""
    steps = next((c for c in cells if c.field == "steps"), None)
    rest = [c for c in cells if c.field != "steps"]
    if steps is None or not any(isinstance(i, Para) and i.text.strip() for i in steps.items):
        return rest
    lead = Para(text=profile.active().steps_label, bold=True)
    items = (lead, *steps.items)
    for index, cell in enumerate(rest):
        if cell.field == "poc":
            rest[index] = replace(cell, items=cell.items + items)
            return rest
    return [*rest, _Cell("poc", steps.text, items)]


def read_detail(tbl, position: int, numbering: dict, counter: Counter) -> RawFinding:
    """One detail table as a ``RawFinding``."""
    cells, first_id = _detail_cells(rows(tbl), numbering, counter)
    values: dict = {"source_id": first_id}
    seen: set[str] = set()
    cut: list[str] = []
    figures: list[Figure] = []
    for cell in _with_steps(cells):
        if cell.field in seen:
            continue  # the first row with a label wins
        seen.add(cell.field)
        found, was_cut, extra = _cell_value(cell)
        values.update({k: v for k, v in found.items() if v or k not in values})
        figures.extend(extra)
        if was_cut:
            cut.append(cell.field)
    return RawFinding(
        position=position,
        origin="detail",
        figures=tuple(figures),
        truncated=tuple(dict.fromkeys(f for f in cut if f in limits.ISSUE_FIELDS)),
        **values,
    )


def _cell_value(cell: _Cell) -> tuple[dict, bool, list[Figure]]:
    if cell.field not in LINE_FIELDS:
        return _rich_value(cell)
    paras, figures = split_figures(pair_figures(list(cell.items)))
    if figures:  # a picture in a one-line cell: keep it, and read the text without its caption
        cell = replace(cell, text="\n".join(p.text for p in paras))
    found, cut = _line_value(cell)
    return found, cut, figures


def _rich_value(cell: _Cell) -> tuple[dict, bool, list[Figure]]:
    if cell.field == "poc":
        items = pair_figures(_strip_poc_label(list(cell.items)))
        return {"poc": tuple(items)}, False, []
    paras, figures = split_figures(pair_figures(list(cell.items)))
    rendered = render(paras)
    html, cut = join_capped(rendered.chunks, limits.MAX_RICH_CHARS)
    return {cell.field: html}, cut or rendered.truncated, figures


def _line_value(cell: _Cell) -> tuple[dict, bool]:
    """The value of a one-line field (id, title, location, urls, severity, CVSS, ...)."""
    field, text = cell.field, cell.text
    if field == "id":
        return {"source_id": source_id(text)}, False
    if field == "title":
        value, cut = one_line(text, limits.MAX_TITLE_CHARS)
        return {"title": value}, cut
    if field in ("location", "parameter", "cwe"):
        value, cut = one_line(text, limits.MAX_LINE_CHARS)
        return {field: value}, cut
    if field == "urls":
        urls, cut = split_urls(text)
        return {"urls": urls}, cut
    if field == "severity":
        return {"severity": severity_of_text(text)}, False
    if field == "cvss":
        vector, score = parse_cvss(text)
        return {"cvss_vector": vector, "cvss_score": score}, False
    if field == "references":
        refs, cut = references(text)
        return {"references": refs}, cut
    return {}, False


# ---------------------------------------------------------------- summary table


def summary_columns(header: list) -> dict[str, int]:
    """Column index per role (id, title, blob, severity, cwe) from the header row."""
    roles: dict[str, int] = {}
    for index, cell in enumerate(header):
        key = label_key(" ".join(cell_text(cell).split("\n")))
        if not key:
            continue
        if "id" not in roles and (key in ID_KEYS or key.startswith("sno")):
            roles["id"] = index
        elif "title" not in roles and ("vulnerability" in key or key in ("title", "finding")):
            roles["title"] = index
        elif "blob" not in roles and any(w in key for w in ("location", "parameter", "url")):
            roles["blob"] = index
        elif "severity" not in roles and ("severity" in key or key.startswith("risk")):
            roles["severity"] = index
        elif "cwe" not in roles and any(w in key for w in ("reference", "cwe", "cve")):
            roles["cwe"] = index
    return roles


def is_summary_table(grid: list[list]) -> bool:
    if len(grid) < 1 or len(grid[0]) < 3:
        return False
    roles = summary_columns(grid[0])
    return "id" in roles and "title" in roles


def parse_blob(text: str) -> dict:
    """``URL: ... Parameter: ... Location: ...`` of a summary cell (with the profile's
    ``blob_lead_urls`` the text before the first label is the URL list, and ``Param:`` is a
    parameter label)."""
    active = profile.active()
    parts = (SUMMARY_LABELS_PARAM if active.blob_param_label else SUMMARY_LABELS).split(text)
    values: dict = {}
    if len(parts) == 1:
        urls, _ = split_urls(text)
        return {"urls": urls}
    if active.blob_lead_urls and split_urls(parts[0])[0]:
        values["urls"] = split_urls(parts[0])[0]
    for index in range(1, len(parts) - 1, 2):
        name = parts[index].lower().rstrip("s")
        name = "parameter" if name == "param" else name
        if name in ("url", "path") and "urls" not in values:
            values["urls"] = split_urls(parts[index + 1])[0]
        elif name in ("parameter", "location") and name not in values:
            values[name] = one_line(parts[index + 1], limits.MAX_LINE_CHARS)[0]
    return values


def read_summary(tbl, position: int) -> list[RawFinding]:
    """One ``RawFinding`` per data row of the summary table."""
    grid = rows(tbl)
    roles = summary_columns(grid[0]) if grid else {}
    found: list[RawFinding] = []
    for offset, row in enumerate(grid[1:]):

        def text(role: str, cells: list = row) -> str:
            index = roles.get(role, -1)
            return cell_text(cells[index]) if 0 <= index < len(cells) else ""

        title = one_line(text("title"), limits.MAX_TITLE_CHARS)[0]
        ident = source_id(text("id"))
        if not title and not ident:
            continue
        found.append(
            RawFinding(
                position=position + offset,
                origin="summary",
                source_id=ident,
                title=title,
                severity=severity_of_text(text("severity")),
                cwe=one_line(text("cwe"), limits.MAX_LINE_CHARS)[0],
                **parse_blob(text("blob")),
            )
        )
    return found
