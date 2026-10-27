"""Which paragraphs go to the model, grouped into chunks that stay inside one section."""

from __future__ import annotations

from dataclasses import dataclass

from . import limits
from .errors import DocxError
from .model import Inspection, Section, Unit


@dataclass(frozen=True)
class Chunk:
    """Refinable paragraphs of one section, small enough for a single model call."""

    section_id: str
    title: str
    units: tuple[Unit, ...]


def select_sections(
    inspection: Inspection, section_ids: tuple[str, ...] | None
) -> tuple[Section, ...]:
    """The chosen sections in document order; ``None`` means every section."""
    if section_ids is None:
        return inspection.sections
    wanted = set(section_ids)
    if not wanted:
        raise DocxError("No section was selected.")
    chosen = tuple(s for s in inspection.sections if s.id in wanted)
    if len(chosen) != len(wanted):
        raise DocxError("An unknown section was selected.")
    return chosen


def _refinable(inspection: Inspection, section: Section) -> list[Unit]:
    units = inspection.units[section.first_unit : section.last_unit + 1]
    return [unit for unit in units if unit.protected is None]


def _section_chunks(section: Section, units: list[Unit]) -> list[Chunk]:
    chunks: list[Chunk] = []
    current: list[Unit] = []
    size = 0
    for unit in units:
        full = (
            size + len(unit.text) > limits.CHUNK_CHARS
            or len(current) >= limits.CHUNK_MAX_PARAGRAPHS
        )
        if current and full:
            chunks.append(Chunk(section.id, section.title, tuple(current)))
            current, size = [], 0
        current.append(unit)
        size += len(unit.text)
    if current:
        chunks.append(Chunk(section.id, section.title, tuple(current)))
    return chunks


def plan_chunks(inspection: Inspection, section_ids: tuple[str, ...] | None) -> tuple[Chunk, ...]:
    """Chunks of at most CHUNK_CHARS characters (a longer paragraph is a chunk of its own)."""
    sections = select_sections(inspection, section_ids)
    if sum(section.chars for section in sections) > limits.MAX_SELECTED_CHARS:
        raise DocxError("The selection is too large to refine in one run; choose fewer sections.")
    chunks: list[Chunk] = []
    for section in sections:
        chunks.extend(_section_chunks(section, _refinable(inspection, section)))
    if len(chunks) > limits.MAX_CHUNKS_PER_RUN:
        raise DocxError(
            "The selection needs too many model calls for one run; choose fewer sections."
        )
    return tuple(chunks)


def split_chunk(chunk: Chunk) -> tuple[Chunk, Chunk]:
    """Two halves of a chunk, in order (for a cut-off answer). Needs at least two paragraphs."""
    if len(chunk.units) < 2:
        raise ValueError("a chunk of one paragraph cannot be split")
    middle = len(chunk.units) // 2
    return (
        Chunk(chunk.section_id, chunk.title, chunk.units[:middle]),
        Chunk(chunk.section_id, chunk.title, chunk.units[middle:]),
    )


def section_body(inspection: Inspection, section: Section, limit: int) -> str:
    """The first ``limit`` characters of the section's refinable text, heading excluded."""
    parts: list[str] = []
    total = 0
    for unit in _refinable(inspection, section):
        if unit.heading_level is not None or total >= limit:
            continue
        parts.append(unit.text)
        total += len(unit.text) + 1
    return " ".join(parts)[:limit]
