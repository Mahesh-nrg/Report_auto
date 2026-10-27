"""The ``structure`` engine: rebuild a clean DOCX from mutool spans (worker side only).

Pipeline per page: lines, running header/footer removal, grid tables, borderless tables,
reading order (one or two columns), images (merged, capped). Then the whole document: body
size, heading plan (title, levels 1-4), blocks, python-docx output, metrics.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import limits
from .blocks import Item, assemble, body_size, page_items, plan_headings
from .emit import write_docx
from .extract import Box, Page
from .lines import Line, column_estimate, make_lines, order_columns, running_key, running_keys
from .metrics import docx_counts, residual_tabular, text_loss
from .model import ConversionMetrics
from .tables import borderless_tables, grid_tables


@dataclass(frozen=True)
class EngineOutput:
    docx: bytes
    metrics: ConversionMetrics
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PageResult:
    items: tuple[Item, ...]
    lines: tuple[Line, ...]  # kept, in reading order
    residual: tuple[Line, ...]  # lines left outside tables (for the tabular measure)
    columns: int
    dropped: tuple[str, ...]
    images_capped: bool


def merge_boxes(boxes: Sequence[Box], gap: float = 4.0) -> list[Box]:
    merged = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        for i in range(len(merged)):
            for j in range(i + 1, len(merged)):
                a, b = merged[i], merged[j]
                if (
                    a[0] - gap <= b[2]
                    and b[0] - gap <= a[2]
                    and a[1] - gap <= b[3]
                    and b[1] - gap <= a[3]
                ):
                    merged[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    merged.pop(j)
                    changed = True
                    break
            if changed:
                break
    return [(b[0], b[1], b[2], b[3]) for b in merged]


def page_images(page: Page, has_text: bool) -> tuple[list[Box], bool]:
    """Placed images worth keeping (merged, at most ``MAX_IMAGES_PER_PAGE``) and whether some
    were dropped by the cap. Tiny images and full-page backgrounds behind text are skipped."""
    small = limits.MIN_IMAGE_POINTS
    area = page.width * page.height
    keep = [
        b
        for b in page.images[:4000]
        if b[2] - b[0] >= small and b[3] - b[1] >= small
        and not (has_text and (b[2] - b[0]) * (b[3] - b[1]) > 0.85 * area)
    ]  # fmt: skip
    merged = merge_boxes(keep[:400])
    return merged[: limits.MAX_IMAGES_PER_PAGE], len(merged) > limits.MAX_IMAGES_PER_PAGE


def analyse_page(page: Page, lines: list[Line], running: frozenset) -> PageResult:
    kept: list[Line] = []
    dropped: list[str] = []
    for line in lines:
        key = running_key(line, page)
        if key is not None and key in running:
            dropped.append(line.text)
        else:
            kept.append(line)
    grid, rest = grid_tables(page, kept)
    loose, rest = borderless_tables(rest)
    ordered, columns = order_columns(rest, page)
    columns = max(columns, column_estimate(rest, page))
    images, capped = page_images(page, bool(kept))
    items = page_items(ordered, grid + loose, images, page.number)
    return PageResult(tuple(items), tuple(ordered), tuple(rest), columns, tuple(dropped), capped)


def analyse(pages: Sequence[Page]) -> tuple[list[PageResult], list[Line]]:
    raw = [make_lines(page) for page in pages]
    running = running_keys(pages, raw)
    results = [analyse_page(page, lines, running) for page, lines in zip(pages, raw, strict=False)]
    flat = [line for result in results for line in result.lines]
    return results, flat


def convert_structure(pdf: Path, pages: Sequence[Page], work: Path, reference: str) -> EngineOutput:
    """Rebuild the PDF as a DOCX. ``reference`` is the plain ``pdftotext`` text."""
    started = time.monotonic()
    results, flat = analyse(pages)
    body = body_size(flat)
    plan = plan_headings(flat, body, len(pages))
    blocks = assemble([r.items for r in results], plan, body)
    size = (pages[0].width, pages[0].height) if pages else (612.0, 792.0)
    data, emitted = write_docx(blocks, pdf, work, size)
    counts = docx_counts(data)
    dropped = [text for r in results for text in r.dropped]
    capped = emitted.images_capped or any(r.images_capped for r in results)
    metrics = ConversionMetrics(
        engine="structure",
        pages=len(pages),
        lines=len(flat),
        headings=counts.headings,
        title=counts.title,
        tables=counts.tables,
        images=counts.images,
        lists=counts.lists,
        text_loss=text_loss(reference, [*counts.texts, *dropped]) if reference else 0.0,
        residual_tabular=residual_tabular(r.residual for r in results),
        max_columns=min(50, max((r.columns for r in results), default=1)),
        dropped_running_lines=len(dropped),
        seconds=round(min(time.monotonic() - started, 3600.0), 2),
    )
    warnings = ("running_lines_removed",) if dropped else ()
    warnings += ("images_capped",) if capped else ()
    return EngineOutput(data, metrics, warnings)


__all__ = ["EngineOutput", "analyse", "convert_structure", "merge_boxes", "page_images"]
