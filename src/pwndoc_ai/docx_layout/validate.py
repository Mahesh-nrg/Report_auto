"""inspect_layout: the issues of a Word document that make blank pages, orphans and white space.

Issue codes: EMPTY_RUN, EMPTY_NEAR_BREAK, DOUBLE_PAGE_BREAK, BREAK_AT_EDGE, EMPTY_SECTION,
ORPHAN_HEADING_RISK, TRAILING_EMPTY, FONT_SUBSTITUTED, FONT_CHECK_UNAVAILABLE, TRACKED_CHANGES,
TOC_STALE_RISK. Messages never quote the document; ``where`` is a paragraph position or at most
40 characters of a heading.
"""

from __future__ import annotations

from . import budget
from .context import Analysis, analyze
from .fonts import Resolver, check_fonts
from .groups import Gap, build_gaps, is_anchor
from .load import LoadedDoc, load_document
from .model import FontIssue, LayoutIssue, LayoutReport
from .ooxml import local
from .scan import EMPTY, Block, Segment, boundary_count
from .sections import empty_sections, orphans, where_text

RUN_LIMIT = 2  # more consecutive empty paragraphs than this is an EMPTY_RUN
MAX_LISTED = 25  # per-heading issues listed at most (the stats keep the full counts)
TRACKED_TAGS = frozenset(
    {"ins", "del", "moveFrom", "moveTo", "pPrChange", "rPrChange", "sectPrChange", "tblPrChange"}
    | {"trPrChange", "tcPrChange", "tblGridChange", "numberingChange", "cellIns", "cellDel"}
)
MESSAGES = {
    "EMPTY_RUN": "Runs of empty paragraphs add white space and can push content to a new page.",
    "EMPTY_NEAR_BREAK": "Empty paragraphs next to a page break or at the top of a page.",
    "DOUBLE_PAGE_BREAK": "Page breaks in a row produce a blank page.",
    "BREAK_AT_EDGE": "A page break at the start or end of the document produces a blank page.",
    "EMPTY_SECTION": "A heading has no content before the next heading.",
    "ORPHAN_HEADING_RISK": "A heading can be left alone at the bottom of a page.",
    "TRAILING_EMPTY": "Empty paragraphs at the end of the document.",
    "FONT_SUBSTITUTED": "The font is not installed; a font with other widths changes the layout.",
    "FONT_CHECK_UNAVAILABLE": "The installed fonts could not be checked.",
    "TRACKED_CHANGES": "The document has tracked changes; it is not tidied automatically.",
    "TOC_STALE_RISK": (
        "The table of contents page numbers are refreshed when rendering to PDF; in Word press F9."
    ),
}
SEVERITY = {
    "EMPTY_RUN": "warning",
    "EMPTY_NEAR_BREAK": "warning",
    "DOUBLE_PAGE_BREAK": "error",
    "BREAK_AT_EDGE": "error",
    "EMPTY_SECTION": "warning",
    "ORPHAN_HEADING_RISK": "warning",
    "TRAILING_EMPTY": "warning",
    "FONT_SUBSTITUTED": "warning",
    "FONT_CHECK_UNAVAILABLE": "info",
    "TRACKED_CHANGES": "info",
    "TOC_STALE_RISK": "info",
}


def make_issue(code: str, *, fixable: bool, count: int = 1, where: str = "") -> LayoutIssue:
    return LayoutIssue(code, SEVERITY[code], MESSAGES[code], fixable, max(1, count), where[:40])


def _place(block: Block) -> str:
    return f"paragraph {block.ordinal}" if block.ordinal else ""


def _empty_runs(blocks: tuple[Block, ...]) -> list[list[Block]]:
    runs: list[list[Block]] = []
    current: list[Block] = []
    for block in (*blocks, None):
        if block is not None and block.kind == EMPTY:
            current.append(block)
            continue
        if current:
            runs.append(current)
        current = []
    return runs


def _aggregate(code: str, blocks: list[Block], content: bool) -> list[LayoutIssue]:
    """One issue for empty paragraphs; fixable when tidy may delete at least one of them (never
    in a body without content, which tidy leaves alone)."""
    if not blocks:
        return []
    fixable = content and any(block.removable for block in blocks)
    return [make_issue(code, fixable=fixable, count=len(blocks), where=_place(blocks[0]))]


def _gap_issues(gaps: tuple[Gap, ...], content: bool) -> list[LayoutIssue]:
    near: list[Block] = []
    trailing: list[Block] = []
    for gap in gaps:
        empties = list(gap.empties)
        if gap.at_end and not gap.at_start:
            required = 1 if gap.needs_separator and not gap.kept_paragraphs else 0
            trailing.extend(empties[required:])
        elif not gap.at_end and (gap.boundaries > 0 or gap.at_start):
            near.extend(empties)
    return [
        *_aggregate("EMPTY_NEAR_BREAK", near, content),
        *_aggregate("TRAILING_EMPTY", trailing, content),
    ]


def blank_segments(segments: tuple[Segment, ...]) -> list[Segment]:
    """Pages that only an explicit boundary creates (a document with one page has none)."""
    return [s for s in segments if s.blank and (s.start is not None or s.end is not None)]


def _break_issues(segments: tuple[Segment, ...], content: bool) -> list[LayoutIssue]:
    double: list[Segment] = []
    edge: list[Segment] = []
    for segment in blank_segments(segments):
        (edge if segment.start is None or segment.end is None else double).append(segment)
    issues: list[LayoutIssue] = []
    for code, found in (("DOUBLE_PAGE_BREAK", double), ("BREAK_AT_EDGE", edge)):
        if found:
            fixable = content and any(_fixable_segment(s) for s in found)
            where = _segment_place(found[0])
            issues.append(make_issue(code, fixable=fixable, count=len(found), where=where))
    return issues


def _fixable_segment(segment: Segment) -> bool:
    return any(b is not None and b.removable for b in (segment.start, segment.end))


def _segment_place(segment: Segment) -> str:
    boundary = segment.end or segment.start
    block = boundary.block if boundary is not None else None
    return _place(block) if block is not None else ""


def _section_issues(analysis: Analysis, gaps: tuple[Gap, ...]) -> tuple[list[LayoutIssue], int]:
    sections = empty_sections(analysis)
    found = [
        make_issue("EMPTY_SECTION", fixable=True, where=where_text(block))
        for block in sections.headings
    ]
    risky = orphans(analysis, gaps)
    found += [
        make_issue("ORPHAN_HEADING_RISK", fixable=item.fixable, where=where_text(item.block))
        for item in risky
    ]
    return found, len(risky)


def has_tracked_changes(doc: LoadedDoc) -> bool:
    return any(local(node) in TRACKED_TAGS for node in doc.body.iter())


def _font_issues(fonts: tuple[FontIssue, ...]) -> list[LayoutIssue]:
    issues = [
        make_issue("FONT_SUBSTITUTED", fixable=False, where=font.font)
        for font in fonts
        if font.resolved and not font.compatible
    ]
    unknown = sum(1 for font in fonts if not font.resolved)
    if unknown:
        issues.append(make_issue("FONT_CHECK_UNAVAILABLE", fixable=False, count=unknown))
    return issues


def _stats(analysis: Analysis, sections: int, orphan_count: int) -> dict[str, int]:
    blocks = analysis.blocks
    paragraphs = [b for b in blocks if b.ordinal]
    runs = _empty_runs(blocks)
    return {
        "paragraphs": len(paragraphs),
        "empty_paragraphs": sum(1 for b in blocks if b.kind == EMPTY),
        "longest_empty_run": max((len(run) for run in runs), default=0),
        "page_breaks": sum(boundary_count(b) for b in paragraphs),
        "section_breaks": sum(1 for b in paragraphs if b.facts.sect is not None),
        "empty_sections": sections,
        "orphan_headings": orphan_count,
        "est_blank_pages": len(blank_segments(analysis.segments)),
    }


def _limited(issues: list[LayoutIssue]) -> list[LayoutIssue]:
    counts: dict[str, int] = {}
    kept: list[LayoutIssue] = []
    for item in issues:
        counts[item.code] = counts.get(item.code, 0) + 1
        if counts[item.code] <= MAX_LISTED:
            kept.append(item)
    return kept


def build_report(analysis: Analysis, fonts: tuple[FontIssue, ...]) -> LayoutReport:
    gaps = build_gaps(analysis.blocks)
    long_runs = [run for run in _empty_runs(analysis.blocks) if len(run) > RUN_LIMIT]
    issues: list[LayoutIssue] = []
    content = any(is_anchor(block) for block in analysis.blocks)
    issues += _aggregate("EMPTY_RUN", [block for run in long_runs for block in run], content)
    issues += _gap_issues(gaps, content) + _break_issues(analysis.segments, content)
    section_issues, orphan_count = _section_issues(analysis, gaps)
    issues += section_issues
    sections = sum(1 for item in section_issues if item.code == "EMPTY_SECTION")
    issues += _font_issues(fonts)
    if has_tracked_changes(analysis.doc):
        issues.append(make_issue("TRACKED_CHANGES", fixable=False))
    if analysis.fields.has_toc:
        issues.append(make_issue("TOC_STALE_RISK", fixable=False))
    stats = _stats(analysis, sections, orphan_count)
    return LayoutReport(tuple(_limited(issues)), fonts, stats)


def inspect_layout(data: bytes, *, font_resolver: Resolver | None = None) -> LayoutReport:
    """Validate a DOCX. Raises DocxLayoutError (user-safe) only for unreadable/hostile files."""
    with budget.limit():
        doc = load_document(data)
        return build_report(analyze(doc), check_fonts(doc, font_resolver))


def is_clean(report: LayoutReport) -> bool:
    """Nothing tidy could fix."""
    return not any(item.fixable for item in report.issues)
