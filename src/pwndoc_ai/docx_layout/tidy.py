"""tidy_docx: fix what is safely fixable in the layout of a Word document.

Phases, each on a fresh analysis of the working copy: drop empty sections (with their table of
contents entries), move a page break that follows a heading above it, clean the gaps between
content (empty runs, redundant breaks), add keep-with-next to headings and captions, then the
opt-in table row and TOC refresh flags. Only word/document.xml is rewritten (and word/settings.xml
for the opt-in TOC flag); every other member is copied unchanged. A document with tracked changes
or with nothing to fix comes back as the same bytes.
"""

from __future__ import annotations

from lxml import etree

from pwndoc_ai.docx_engine.errors import DocxError
from pwndoc_ai.docx_engine.package import write_package

from . import budget
from .context import Analysis, analyze
from .edits import (
    protect_rows,
    remove_element,
    remove_page_breaks,
    set_update_fields,
    switch_off,
    switch_on,
)
from .errors import DocxLayoutError
from .fonts import Resolver, check_fonts
from .groups import build_gaps, gap_after, gap_before
from .load import LoadedDoc, load_document, read_xml
from .model import LayoutIssue, TidyOptions, TidyResult
from .plan import GapPlan, plan_gap
from .scan import TABLE, Block
from .sections import EmptySection, boundaries_ok, empty_sections, orphans, section_end
from .toc_numbers import renumber_entries
from .validate import build_report, inspect_layout, is_clean

Counts = dict[str, int]
PBB, KEEP = "pageBreakBefore", "keepNext"
MAX_SECTION_ROUNDS = 200  # empty sections removed per tidy at most
FIX_MESSAGES = {
    "empty_sections_removed": "Removed headings that had no content.",
    "toc_entries_removed": "Removed the table of contents entries of those headings.",
    "toc_numbers_updated": "Renumbered the table of contents entries after the removed headings.",
    "page_breaks_moved": "Moved a page break above a heading that was alone on its page.",
    "page_breaks_removed": "Removed page breaks that produced blank pages.",
    "page_breaks_converted": "Turned break-only paragraphs into page-break-before.",
    "empty_paragraphs_removed": "Removed stray empty paragraphs.",
    "keep_with_next_added": "Kept headings and captions with the next paragraph.",
    "table_rows_protected": "Kept table rows from splitting across pages.",
    "toc_update_flag_set": "Word refreshes the table of contents when the file is opened.",
    "tracked_changes_skipped": "The document has tracked changes and was not tidied.",
}


def _merge(*parts: Counts) -> Counts:
    keys = {key for part in parts for key in part}
    return {key: sum(part.get(key, 0) for part in parts) for key in keys}


def _runs(
    blocks: tuple[Block, ...], found: tuple[EmptySection, ...]
) -> list[list[tuple[EmptySection, int, int]]]:
    """The sections as (section, first block index, end index), grouped into runs of neighbours
    (one ends where the next begins)."""
    position = {id(block): index for index, block in enumerate(blocks)}
    runs: list[list[tuple[EmptySection, int, int]]] = []
    for section in found:
        index = position[id(section.heading)]
        item = (section, index, section_end(blocks, index))
        if runs and runs[-1][-1][2] == index:
            runs[-1].append(item)
        else:
            runs.append([item])
    return runs


def _alternate(run: list[tuple[EmptySection, int, int]]) -> list[EmptySection]:
    """Every other section of a run, from its end: none ends where a chosen one begins, so each
    gap is the one that was checked on its own."""
    chosen: list[EmptySection] = []
    starts: set[int] = set()
    for section, index, end in reversed(run):
        if end not in starts:
            starts.add(index)
            chosen.append(section)
    chosen.reverse()
    return chosen


def _independent(analysis: Analysis, found: tuple[EmptySection, ...]) -> list[EmptySection]:
    """The sections that can go in one pass. Neighbouring sections go together when removing
    all of them leaves no blank page (checked as one gap), else every other one goes."""
    chosen: list[EmptySection] = []
    for run in _runs(analysis.blocks, found):
        if len(run) == 1 or boundaries_ok(analysis.blocks, run[0][1], run[-1][2]):
            chosen.extend(item[0] for item in run)
        else:
            chosen.extend(_alternate(run))
    return chosen


def _drop_round(doc: LoadedDoc) -> Counts:
    """Remove the removable empty sections of a fresh analysis (removals interact, so the next
    round analyses again)."""
    analysis = analyze(doc)
    sections = _independent(analysis, empty_sections(analysis).sections)
    if not sections:
        return {}
    headings = [block for section in sections for block in section.headings]
    renumbered = renumber_entries(analysis, headings)
    for section in sections:
        for block in (*section.headings, *section.empties):
            remove_element(block.element)
        for entry in section.toc_entries:
            remove_element(entry)
    return {
        "empty_sections_removed": len(headings),
        "toc_entries_removed": sum(len(s.toc_entries) for s in sections),
        "empty_paragraphs_removed": sum(len(s.empties) for s in sections),
        "toc_numbers_updated": renumbered,
    }


def _drop_sections(doc: LoadedDoc) -> Counts:
    counts: Counts = {}
    for _ in range(MAX_SECTION_ROUNDS):
        budget.check()
        removed = _drop_round(doc)
        if not removed:
            break
        counts = _merge(counts, removed)
    return counts


def _move_orphan_breaks(doc: LoadedDoc) -> Counts:
    analysis = analyze(doc)
    gaps = build_gaps(analysis.blocks)
    moved = 0
    for item in orphans(analysis, gaps):
        after = gap_after(gaps, item.block)
        if not item.movable or after is None:
            continue
        for block in after.breaks:
            remove_element(block.element)
        before = gap_before(gaps, item.block)
        if before is not None and not before.at_start and before.boundaries == 0:
            switch_on(item.block.element, PBB)
        moved += 1
    return {"page_breaks_moved": moved}


def _apply_gap(plan: GapPlan) -> None:
    for block in plan.remove:
        remove_element(block.element)
    if plan.pbb_on is not None:
        switch_on(plan.pbb_on.element, PBB)
    if plan.normalize is not None:
        remove_page_breaks(plan.normalize.element)
        switch_on(plan.normalize.element, PBB)
    if plan.strip is not None:
        remove_page_breaks(plan.strip.element)
        if plan.strip.facts.pbb:
            switch_off(plan.strip.element, PBB)


def _tidy_gaps(doc: LoadedDoc, options: TidyOptions) -> Counts:
    plans = [plan_gap(gap, options) for gap in build_gaps(analyze(doc).blocks)]
    changed = [plan for plan in plans if plan.changes]
    for plan in changed:
        _apply_gap(plan)
    return {
        "empty_paragraphs_removed": sum(p.empties_removed for p in changed),
        "page_breaks_removed": sum(p.breaks_removed for p in changed),
        "page_breaks_converted": sum(p.converted for p in changed),
    }


def _keep_with_next(doc: LoadedDoc) -> Counts:
    analysis = analyze(doc)
    risky = orphans(analysis, build_gaps(analysis.blocks))
    added = sum(1 for item in risky if item.missing_keep and switch_on(item.block.element, KEEP))
    return {"keep_with_next_added": added}


def _protect_tables(doc: LoadedDoc) -> Counts:
    tables = [b.element for b in analyze(doc).blocks if b.kind == TABLE]
    return {"table_rows_protected": sum(protect_rows(table) for table in tables)}


def _layout_phases(doc: LoadedDoc, options: TidyOptions) -> Counts:
    counts: Counts = {}
    if options.drop_empty_sections:
        counts = _merge(counts, _drop_sections(doc))
    if options.keep_headings_with_next and options.fix_page_breaks:
        counts = _merge(counts, _move_orphan_breaks(doc))
    if options.collapse_empty_runs or options.fix_page_breaks:
        counts = _merge(counts, _tidy_gaps(doc, options))
    if options.keep_headings_with_next:
        counts = _merge(counts, _keep_with_next(doc))
    return counts


def _settings_update(doc: LoadedDoc) -> dict[str, bytes]:
    names = doc.parts.get("settings", ())
    if not names or not analyze(doc).fields.has_toc:
        return {}
    settings = read_xml(doc.package, names[0])
    if settings is None or not set_update_fields(settings):
        return {}
    return {names[0]: _serialize(settings)}


def _serialize(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _fixes(counts: Counts) -> tuple[LayoutIssue, ...]:
    return tuple(
        LayoutIssue(code, "info", message, True, counts[code])
        for code, message in FIX_MESSAGES.items()
        if counts.get(code, 0) > 0
    )


def _write(doc: LoadedDoc, extra: dict[str, bytes]) -> bytes:
    try:
        return write_package(doc.package, {doc.package.main_part: _serialize(doc.root), **extra})
    except DocxError as exc:
        raise DocxLayoutError(str(exc)) from None


def tidy_docx(
    data: bytes, options: TidyOptions | None = None, *, font_resolver: Resolver | None = None
) -> TidyResult:
    """Tidy a DOCX (see the module docstring). Raises DocxLayoutError for unreadable files."""
    with budget.limit():
        return _tidy(data, options or TidyOptions(), font_resolver)


def _tidy(data: bytes, chosen: TidyOptions, font_resolver: Resolver | None) -> TidyResult:
    doc = load_document(data)
    before = build_report(analyze(doc), check_fonts(doc, font_resolver))
    if any(item.code == "TRACKED_CHANGES" for item in before.issues):
        skipped = _fixes({"tracked_changes_skipped": 1})
        return TidyResult(data, skipped, before, before)
    counts = {} if is_clean(before) else _layout_phases(doc, chosen)
    if chosen.keep_table_rows_together:
        counts = _merge(counts, _protect_tables(doc))
    extra = _settings_update(doc) if chosen.update_toc_flag else {}
    counts = _merge(counts, {"toc_update_flag_set": len(extra)})
    if not any(counts.values()):
        return TidyResult(data, (), before, before)
    tidied = _write(doc, extra)
    after = inspect_layout(tidied, font_resolver=font_resolver)
    return TidyResult(tidied, _fixes(counts), before, after)
