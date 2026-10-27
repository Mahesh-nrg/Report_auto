"""Quality measures of a conversion: text loss against ``pdftotext``, residual tabular lines,
and the counts read back from a produced DOCX."""

from __future__ import annotations

import io
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .extract import ToolError, run_tool
from .lines import Line
from .tables import baseline_rows

TOKEN = re.compile(r"\w{2,}")
TABULAR_GAP_EM = 2.5
TABULAR_CELLS = 3
HEADING_STYLE = re.compile(r"heading ([1-9])", re.I)
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def tokens(text: str) -> Counter:
    """NFKC (ligatures split), case-folded tokens of two or more word characters."""
    return Counter(TOKEN.findall(unicodedata.normalize("NFKC", text).casefold()))


def text_loss(reference: str, produced: Iterable[str]) -> float:
    """Share of reference tokens (with multiplicity) missing from the produced text."""
    want = tokens(reference)
    total = sum(want.values())
    if not total:
        return 0.0
    have: Counter = Counter()
    for part in produced:
        have.update(tokens(part))
    missing = sum(max(0, count - have[token]) for token, count in want.items())
    return round(min(1.0, missing / total), 4)


def pdftotext(pdf: Path) -> str:
    """Plain ``pdftotext`` output (the loss oracle); empty when the tool fails."""
    try:
        return run_tool(["pdftotext", "-q", "-enc", "UTF-8", str(pdf), "-"]).decode(
            "utf-8", "replace"
        )
    except ToolError:
        return ""


def _tabular(row: Sequence[Line]) -> bool:
    if len(row) < TABULAR_CELLS:
        return False
    ordered = sorted(row, key=lambda ln: ln.x0)
    em = max(1.0, max(ln.size for ln in row))
    return all(
        b.x0 - a.x1 > TABULAR_GAP_EM * em for a, b in zip(ordered, ordered[1:], strict=False)
    )


def residual_tabular(pages: Iterable[Sequence[Line]]) -> float:
    """Share of lines that sit in rows of 3+ spans separated by more than 2.5 em, outside
    any detected table (the lines given are the ones left after table detection)."""
    total = hits = 0
    for lines in pages:
        total += len(lines)
        hits += sum(len(row) for row in baseline_rows(lines) if _tabular(row))
    return round(hits / total, 4) if total else 0.0


@dataclass(frozen=True)
class DocxCounts:
    paragraphs: int
    headings: tuple[int, int, int, int]
    title: int
    tables: int
    images: int
    lists: int
    texts: tuple[str, ...]


def _paragraph_text(paragraph) -> str:  # noqa: ANN001 - lxml element
    return "".join(node.text or "" for node in paragraph.iter(f"{W}t"))


def docx_counts(data: bytes) -> DocxCounts:
    """Counts and texts of a DOCX (body paragraphs, table cells included)."""
    import docx

    document = docx.Document(io.BytesIO(data))
    styles = {s.style_id: (s.name or "") for s in document.styles}
    levels = [0, 0, 0, 0]
    title = lists = 0
    texts: list[str] = []
    body = document.element.body
    for paragraph in body.iter(f"{W}p"):
        text = _paragraph_text(paragraph)
        texts.append(text)
        style_node = paragraph.find(f"{W}pPr/{W}pStyle")
        name = styles.get(style_node.get(f"{W}val"), "") if style_node is not None else ""
        match = HEADING_STYLE.fullmatch(name)
        if match and text.strip():
            levels[min(4, int(match.group(1))) - 1] += 1
        title += name == "Title" and bool(text.strip())
        lists += name.startswith("List") or paragraph.find(f"{W}pPr/{W}numPr") is not None
    images = sum(1 for _ in body.iter(f"{W}drawing"))
    return DocxCounts(
        paragraphs=sum(1 for t in texts if t.strip()),
        headings=(levels[0], levels[1], levels[2], levels[3]),
        title=title,
        tables=len(body.findall(f"{W}tbl")),
        images=images,
        lists=lists,
        texts=tuple(texts),
    )


def fonts_seen(fonts: Iterable[tuple[str, int]], limit: int = 20) -> tuple[str, ...]:
    """Most used PDF font families (by characters), reduced by ``strip_pdf_font_name`` and kept
    only when they pass ``clean_family`` (hints for the UI, never trusted)."""
    from ..export_errors import ExportInputError
    from ..font_catalog_model import clean_family, squash, strip_pdf_font_name

    weights: Counter = Counter()
    names: dict[str, str] = {}
    for raw, chars in fonts:
        try:
            name = clean_family(strip_pdf_font_name(raw))
        except ExportInputError:
            continue
        if name:
            key = squash(name)
            names.setdefault(key, name)
            weights[key] += chars
    return tuple(names[key] for key, _ in weights.most_common(limit) if key)


__all__ = [
    "DocxCounts",
    "docx_counts",
    "fonts_seen",
    "pdftotext",
    "residual_tabular",
    "text_loss",
    "tokens",
]
