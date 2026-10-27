"""Public data types of the DOCX engine (frozen, except the progress snapshot)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Unit:
    """One body paragraph. ``text`` carries TAB and NEWLINE characters for tabs and line breaks."""

    id: str
    text: str
    style: str
    heading_level: int | None
    protected: str | None = None  # why the engine will not edit it, or None when refinable
    in_table: bool = False
    addable: bool = True  # whether a paragraph can be added after it and rejected cleanly


@dataclass(frozen=True)
class Section:
    """Paragraphs from one heading up to the next heading (``s0`` may precede the first heading)."""

    id: str
    title: str
    level: int
    units: int  # refinable paragraphs
    chars: int  # characters of refinable text
    first_unit: int  # index into Inspection.units, inclusive
    last_unit: int  # inclusive


@dataclass(frozen=True)
class Inspection:
    units: tuple[Unit, ...]
    sections: tuple[Section, ...]
    stats: dict[str, int]


@dataclass(frozen=True)
class RefineRequest:
    mode: str  # "light" (copy-edit) or "deep" (also research and strengthen)
    section_ids: tuple[str, ...] | None = None  # None: the whole document
    client_terms: tuple[str, ...] = ()
    language_hint: str = ""


@dataclass(frozen=True)
class Change:
    """One paragraph outcome. An added paragraph has ``before == ""`` and ``unit_id`` of the
    paragraph it follows."""

    unit_id: str
    section_id: str
    before: str
    after: str
    status: str  # "applied", "rejected" or "unchanged"
    reason: str = ""


@dataclass(frozen=True)
class RefineResult:
    clean: bytes
    tracked: bytes
    changes: tuple[Change, ...]
    stats: dict[str, object]


@dataclass
class Progress:
    total_chunks: int = 0
    done_chunks: int = 0
    changed: int = 0
    rejected: int = 0
    protected: int = 0
    current: str = ""  # title of the section being worked on
