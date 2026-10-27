"""The deterministic, label-driven reader: a DOCX built from the active PwnDoc template in, an
``ImportedReport`` out. No LLM, no network, no PwnDoc.

The file is hostile input: it is opened with ``docx_engine.package.open_package`` (zip-bomb,
member and macro checks) and parsed with ``docx_engine.xmlutil.parse_xml`` (no entities, no DTD,
element cap), and the whole read runs under a wall-clock budget. lxml is only reached through
those modules, imported when ``read_report`` runs, so this package imports without it.
"""

from __future__ import annotations

import importlib
import itertools
from dataclasses import dataclass, field, replace

from . import limits, profile, profile_paradigm, reader_xml
from .dedupe import Merged, dedupe_figures, dedupe_findings
from .errors import DocumentUnreadable, ImportEngineMissing, ImportLimitError
from .findings import (
    Figure,
    RawFinding,
    is_detail_table,
    is_summary_table,
    pair_figures,
    read_detail,
    read_summary,
)
from .header_fields import FrontTables, HeaderResult, classify, read_header, severity_mismatch
from .labels import label_key
from .model import CompletenessIssue, ImportedReport, SourceStats
from .reader_assemble import assemble
from .reader_media import Media
from .reader_xml import (
    T_P,
    Counter,
    all_paragraphs,
    blocks,
    numbering_map,
    para_text,
    read_paragraph,
    relationships,
    rels_name,
    rows,
    tables_within,
    textbox_paragraphs,
    w,
)

LIMIT_WORDS = ("too large", "too many", "complex")


@dataclass(frozen=True)
class Document:
    package: object
    root: object
    numbering: dict
    rels: dict
    headers: tuple  # per page header part: its paragraphs


@dataclass
class _Scan:
    """What one pass over the body collected (internal, filled once then read)."""

    raw: list[RawFinding] = field(default_factory=list)
    trailing: dict[int, list] = field(default_factory=dict)
    front: dict[str, list] = field(default_factory=dict)
    prose: list[str] = field(default_factory=list)
    finding_tables: int = 0
    detail_tables: int = 0
    summary_rows: int = 0


def _engine():
    try:
        package = importlib.import_module("pwndoc_ai.docx_engine.package")
        xmlutil = importlib.import_module("pwndoc_ai.docx_engine.xmlutil")
        engine_limits = importlib.import_module("pwndoc_ai.docx_engine.limits")
        errors = importlib.import_module("pwndoc_ai.docx_engine.errors")
    except ImportError:
        raise ImportEngineMissing() from None
    return package, xmlutil, engine_limits, errors.DocxError


def _refused(exc: Exception) -> Exception:
    text = str(exc).lower()
    if any(word in text for word in LIMIT_WORDS):
        return ImportLimitError("too_large")
    return DocumentUnreadable("unreadable")


def _optional_xml(package, name: str, xmlutil, cap: int, docx_error: type):
    if not name or name not in package.names:
        return None
    try:
        return xmlutil.parse_xml(package.read(name, cap))
    except docx_error:
        return None  # a damaged auxiliary part is ignored, never trusted


def _open(data: bytes) -> Document:
    package_mod, xmlutil, engine_limits, docx_error = _engine()
    try:
        package = package_mod.open_package(data)
        root = xmlutil.parse_xml(package.read(package.main_part, engine_limits.MAX_XML_BYTES))
    except docx_error as exc:
        raise _refused(exc) from None
    if root.find(w("body")) is None:
        raise DocumentUnreadable("unreadable")
    if sum(1 for _ in root.iter(T_P)) > engine_limits.MAX_PARAGRAPHS:
        raise ImportLimitError("too_large")
    names = frozenset(package.names)
    small = engine_limits.MAX_SMALL_XML_BYTES
    rels_root = _optional_xml(package, rels_name(package.main_part), xmlutil, small, docx_error)
    rels = relationships(rels_root, package.main_part, names) if rels_root is not None else {}
    targets = {rel.kind: rel.target for rel in reversed(list(rels.values()))}
    numbering_root = _optional_xml(
        package, targets.get("numbering", "word/numbering.xml"), xmlutil, small, docx_error
    )
    headers = []
    for rel in rels.values():
        if rel.kind == "header":
            part = _optional_xml(package, rel.target, xmlutil, small, docx_error)
            if part is not None:
                headers.append(tuple(all_paragraphs(part)))
    numbering = numbering_map(numbering_root) if numbering_root is not None else {}
    return Document(package, root, numbering, rels, tuple(headers))


def _table(scan: _Scan, doc: Document, tbl, position: int, counter: Counter) -> bool:
    """Read one table; True when it was a detail table (it opens the finding region)."""
    grid = rows(tbl)
    if is_detail_table(grid):
        scan.raw.append(read_detail(tbl, position, doc.numbering, counter))
        scan.finding_tables += 1
        scan.detail_tables += 1
        return True
    if is_summary_table(grid):
        found = read_summary(tbl, position)
        scan.raw.extend(found)
        scan.finding_tables += 1
        scan.summary_rows += len(found)
        return False
    kind = classify(grid)
    scan.front.setdefault(kind, []).append(grid)
    opened = False
    if kind == "other":
        for offset, inner in enumerate(tables_within(tbl), start=1):
            if is_detail_table(rows(inner)) or is_summary_table(rows(inner)):
                opened = _table(scan, doc, inner, position + offset, counter) or opened
    return opened


def _guard(scan: _Scan) -> None:
    if len(scan.raw) > 2 * limits.MAX_FINDINGS + 2:
        raise ImportLimitError("too_many_findings")


def _scan(doc: Document, counter: Counter) -> _Scan:
    scan = _Scan()
    # index in scan.raw of the detail table whose trailing pictures we collect: every picture
    # after the first detail table goes to the nearest one before it (flagged unmatched), a
    # Heading 1 or a later section included, so no screenshot is ever dropped
    region = -1
    for index, block in enumerate(blocks(doc.root.find(w("body")))):
        position = index * 1000
        if block.tag == T_P:
            items = read_paragraph(block, doc.numbering, counter)
            text = "".join(i.text for i in items if hasattr(i, "text"))
            scan.prose.append(text)
            if region >= 0 and _ends_findings(text):
                region = -1  # the profile's back matter: its pictures are not evidence
            if region >= 0:
                scan.trailing.setdefault(region, []).extend(items)
            continue
        if _table(scan, doc, block, position, counter):
            region = len(scan.raw) - 1
        _guard(scan)
    return scan


def _ends_findings(text: str) -> bool:
    markers = profile.active().end_markers
    return bool(markers) and label_key(text) in markers


def _detect(doc: Document) -> profile.LayoutProfile:
    """The layout profile of the document, from its page header and body paragraphs."""
    texts = (para_text(p, doc.numbering) for part in doc.headers for p in part)
    body = (para_text(p, doc.numbering) for p in all_paragraphs(doc.root))
    return profile.detect(itertools.chain(texts, body))


def _read_header(doc: Document, scan: _Scan, textboxes: list[str]) -> HeaderResult:
    if profile.active() is profile.PARADIGM:
        return profile_paradigm.read_header(doc.headers, _front(scan), textboxes, scan.prose)
    return read_header(doc.headers, _front(scan), textboxes, scan.prose)


def _total_mismatch(scan: _Scan, built) -> bool:
    if profile.active() is not profile.PARADIGM:
        return False
    total = profile_paradigm.printed_total(_front(scan))
    return total is not None and total != sum(1 for f in built.findings if f.include)


def _with_trailing(scan: _Scan) -> list[RawFinding]:
    raw = list(scan.raw)
    for index, items in scan.trailing.items():
        figures = [i for i in pair_figures(items, unmatched=True) if isinstance(i, Figure)]
        if figures:
            raw[index] = replace(raw[index], figures=raw[index].figures + tuple(figures))
    return raw


def _front(scan: _Scan) -> FrontTables:
    return FrontTables(**{kind: tuple(grids) for kind, grids in scan.front.items()})


def _dedupe_all(merged: list[Merged], media: Media) -> tuple[list[Merged], int]:
    out: list[Merged] = []
    removed = 0
    for item in merged:
        finding = item.finding
        poc, dropped = dedupe_figures([*finding.poc, *finding.figures], media.sha_of)
        removed += dropped
        out.append(replace(item, finding=replace(finding, poc=tuple(poc), figures=())))
    return out, removed


def _source_issues(merged: list[Merged], removed: int, gaps: tuple) -> list:
    copies = sum(m.copies - 1 for m in merged)
    issues = [
        CompletenessIssue("id_gaps") if gaps else None,
        CompletenessIssue("duplicates_merged") if copies else None,
        CompletenessIssue("figures_deduped") if removed else None,
    ]
    return [i for i in issues if i is not None]


def _id_stats(merged: list[Merged]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    ids = sorted({m.finding.source_id for m in merged if m.finding.source_id}, key=int)
    cap = 2 * limits.MAX_FINDINGS
    if not ids:
        return (), ()
    width = max(len(i) for i in ids)
    numbers = {int(i) for i in ids}
    top = max(numbers)
    gaps: list[str] = []
    if top <= cap:
        gaps = [str(n).zfill(width) for n in range(1, top) if n not in numbers]
    return tuple(ids[:cap]), tuple(gaps[:cap])


def _report(scan: _Scan, merged: list[Merged], removed: int, header: HeaderResult, built):
    ids, gaps = _id_stats(merged)
    issues = [*_source_issues(merged, removed, gaps), *header.issues, *built.finding_issues]
    if severity_mismatch(header.header.severity_table, built.findings) or _total_mismatch(
        scan, built
    ):
        issues.append(CompletenessIssue("severity_table_mismatch"))
    issues.extend(built.image_issues)
    stats = SourceStats(
        summary_rows=scan.summary_rows,
        detail_tables=scan.detail_tables,
        merged=sum(m.copies - 1 for m in merged),
        figures_deduped=removed,
        source_ids=ids,
        id_gaps=gaps,
    )
    return ImportedReport(
        header=header.header,
        findings=built.findings,
        images=built.images,
        blobs=built.blobs,
        issues=tuple(issues[: limits.MAX_ISSUES]),
        stats=stats,
        header_text=header.header_text,
    )


def read_report(data: bytes, *, file_name: str) -> ImportedReport:
    """Read a report built from the active template. Raises ``DocumentUnreadable``
    (``unreadable``, ``not_template``, ``no_findings``) or ``ImportLimitError``
    (``too_large``, ``too_many_findings``, ``too_many_images``). ``file_name`` is not read
    for any field (the plan derives the audit name from it)."""
    with reader_xml.budget():
        doc = _open(data)
        with profile.use(_detect(doc)):
            return _read(doc)


def _read(doc: Document) -> ImportedReport:
    counter = Counter()
    scan = _scan(doc, counter)
    if not scan.finding_tables:
        raise DocumentUnreadable("not_template")
    if not scan.raw:
        raise DocumentUnreadable("no_findings")
    merged = dedupe_findings(_with_trailing(scan))
    if len(merged) > limits.MAX_FINDINGS:
        raise ImportLimitError("too_many_findings")
    media = Media(doc.package, doc.rels)
    merged, removed = _dedupe_all(merged, media)
    textboxes = [para_text(p, doc.numbering) for p in textbox_paragraphs(doc.root)]
    header = _read_header(doc, scan, textboxes)
    built = assemble(merged, media)
    if len(built.images) > limits.MAX_IMAGES:
        raise ImportLimitError("too_many_images")
    return _report(scan, merged, removed, header, built)
