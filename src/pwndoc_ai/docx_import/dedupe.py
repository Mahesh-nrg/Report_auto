"""Merge the two copies of each finding, and drop repeated figures (no lxml).

The template prints every finding twice: a row of the summary table and a detail table. Copies
are the same finding when they share the printed id (and their titles do not contradict), or
when their folded titles are equal (and their ids do not contradict). Ids compare as numbers
(``001`` is ``1``). The richest copy (most non-blank fields, then most characters) is kept; its
blank fields are filled from the others, the URLs are united in order and differing locations
and parameters are joined (``a, b``). Copies that disagree on severity, CWE or CVSS are marked
``disagree`` so the preview asks the user to check the kept value. A summary row with no detail
copy stays, flagged ``summary_only``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from . import limits, profile
from .captions import caption_key
from .findings import Figure, RawFinding
from .html import Para
from .labels import fold_title

SCALARS = (
    "source_id", "title", "severity", "cvss_vector", "cvss_score", "cwe", "parameter",
    "location", "description", "observation", "remediation",
)  # fmt: skip
UNITED = ("location", "parameter")  # differing values are joined, never dropped
CHECKED = ("severity", "cwe", "cvss_vector", "cvss_score")  # differing values are flagged
ORPHAN = re.compile(
    r"(?is)^[\s\-*\u2022\u2013\u2014]*(?:references?\s*:?)?[\s\-*\u2022\u2013\u2014]*$"
)


@dataclass(frozen=True)
class Merged:
    finding: RawFinding
    copies: int  # how many copies were merged into it (1 = unique)
    has_detail: bool
    disagree: bool = False  # the copies printed different severity, CWE or CVSS values


def _poc_filled(finding: RawFinding) -> bool:
    return any(
        isinstance(i, Figure) or (isinstance(i, Para) and i.text.strip()) for i in finding.poc
    )


def richness(finding: RawFinding) -> tuple[int, int]:
    """(non-blank fields, characters) used to pick the copy that is kept."""
    filled = sum(1 for name in SCALARS if getattr(finding, name))
    filled += bool(finding.urls) + bool(finding.references) + _poc_filled(finding)
    chars = sum(len(str(getattr(finding, name))) for name in SCALARS)
    chars += sum(map(len, finding.urls)) + sum(map(len, finding.references))
    chars += sum(len(i.text) for i in finding.poc if isinstance(i, Para))
    return filled, chars


def id_key(source_id: str) -> str:
    """A printed id for comparing: ``001`` and ``1`` are the same finding number."""
    return str(int(source_id)) if source_id.isdigit() else source_id


def _same(a: RawFinding, b: RawFinding) -> bool:
    ida, idb = id_key(a.source_id), id_key(b.source_id)
    ids_conflict = bool(ida and idb and ida != idb)
    ta, tb = fold_title(a.title), fold_title(b.title)
    if ta and ta == tb:
        return not ids_conflict
    if ida and ida == idb:
        return not (ta and tb) or a.origin != b.origin
    return False


def _groups(raw: Sequence[RawFinding]) -> list[list[int]]:
    parent = list(range(len(raw)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    by_title: dict[str, int] = {}
    by_id: dict[str, list[int]] = {}
    for index, finding in enumerate(raw):
        candidates = [
            by_title.get(fold_title(finding.title), -1),
            *by_id.get(id_key(finding.source_id), []),
        ]
        for other in candidates:
            if other >= 0 and _same(raw[other], finding):
                parent[root(index)] = root(other)
        if fold_title(finding.title):
            by_title.setdefault(fold_title(finding.title), index)
        if finding.source_id:
            by_id.setdefault(id_key(finding.source_id), []).append(index)
    groups: dict[int, list[int]] = {}
    for index in range(len(raw)):
        groups.setdefault(root(index), []).append(index)
    return sorted(groups.values(), key=lambda g: min(raw[i].position for i in g))


def _distinct(copies: Sequence[RawFinding], name: str) -> list[str]:
    values: dict[str, str] = {}
    for copy in copies:
        value = " ".join(str(getattr(copy, name)).split())
        if value:
            values.setdefault(value.casefold(), value)
    return list(values.values())


def disagree(copies: Sequence[RawFinding]) -> bool:
    """True when the copies print different severity, CWE or CVSS values."""
    return any(len(_distinct(copies, name)) > 1 for name in CHECKED)


def _united(ranked: Sequence[RawFinding], name: str) -> str:
    joined = ", ".join(_distinct(ranked, name))
    return joined if len(joined) <= limits.MAX_LINE_CHARS else getattr(ranked[0], name)


def _merge(copies: list[RawFinding]) -> RawFinding:
    ranked = sorted(copies, key=richness, reverse=True)
    best = ranked[0]
    values: dict = {}
    for name in SCALARS:
        if not getattr(best, name):
            values[name] = next((getattr(c, name) for c in ranked if getattr(c, name)), "")
    detail = next((c for c in ranked if c.origin == "detail"), None)
    if profile.active().detail_wins and detail is not None:
        values.update((n, getattr(detail, n) or values.get(n, getattr(best, n))) for n in UNITED)
        urls = list(detail.urls or next((c.urls for c in ranked if c.urls), ()))
    else:
        values.update((name, _united(ranked, name)) for name in UNITED)
        urls = list(dict.fromkeys(url for copy in ranked for url in copy.urls))
    if not best.references:
        values["references"] = next((c.references for c in ranked if c.references), ())
    poc_source = best if _poc_filled(best) else next((c for c in ranked if _poc_filled(c)), best)
    figures = [
        fig
        for copy in ranked
        if copy is not poc_source
        for fig in copy.poc
        if isinstance(fig, Figure)
    ]
    cut = tuple(dict.fromkeys(name for copy in ranked for name in copy.truncated))
    return replace(
        best,
        **values,
        urls=tuple(urls),
        poc=poc_source.poc,
        figures=tuple(f for copy in ranked for f in copy.figures) + tuple(figures),
        truncated=cut,
        position=min(c.position for c in copies),
    )


def dedupe_findings(raw: Sequence[RawFinding]) -> list[Merged]:
    """One ``Merged`` per distinct finding, in order of first appearance."""
    merged: list[Merged] = []
    for group in _groups(raw):
        copies = [raw[i] for i in group]
        finding = copies[0] if len(copies) == 1 else _merge(copies)
        has_detail = any(c.origin == "detail" for c in copies)
        merged.append(Merged(finding, len(copies), has_detail, disagree(copies)))
    return merged


def dedupe_figures(items: Sequence, sha_of: Callable[[str], str]) -> tuple[list, int]:
    """Drop a figure that repeats an earlier one of the same list (same bytes and same
    caption), together with the orphan "References:" / "-" text directly before it."""
    seen: set[tuple[str, str]] = set()
    out: list = []
    removed = 0
    for item in items:
        if isinstance(item, Figure):
            key = (sha_of(item.rid), caption_key(item.caption))
            if key[0] and key in seen:
                removed += 1
                while out and isinstance(out[-1], Para) and ORPHAN.fullmatch(out[-1].text):
                    out.pop()
                continue
            seen.add(key)
        out.append(item)
    return out, removed
