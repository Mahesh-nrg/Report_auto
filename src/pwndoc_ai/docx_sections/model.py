"""Data model of section detection: blocks, labels, sections, the LLM tool and its validation.

Pure data (no lxml). ``from_json`` treats input as hostile (a stored file that may have been
tampered with, or an LLM answer). Sections are never trusted from storage: they are *derived*
from the labels by :func:`build_sections`, and a stored ``sections`` list must equal that
derivation exactly. The AI only ever returns block ids and block types, never document text.

Contract: docs/EXPORT_DESIGN.md (h).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .. import export_json as jc
from ..block_types import BLOCK_TYPES, HEADING_TYPES, heading_level
from ..font_catalog_model import SIZE_MAX, clean_family
from .errors import LabelAnswerRejected

SECTIONS_VERSION = 1
BLOCK_ID = re.compile(r"b[0-9]{5}")
SECTION_ID = re.compile(r"s[1-9][0-9]{0,4}")
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:/@+-]{0,99}")
BLOCK_KINDS = ("paragraph", "table")
SOURCES = ("ai", "heuristic", "style")
METHODS = ("ai", "heuristic", "mixed")
WARNINGS = (
    "ai_disabled",  # the user switched the AI off
    "llm_unavailable",  # no LLM configured: heuristic only
    "llm_failed",  # every model in the chain failed: heuristic for the rest
    "llm_key",  # the LLM refused the key (401/403): heuristic, admin must fix Settings
    "answer_rejected",  # at least one batch answer failed validation and was repaired
    "partial_coverage",  # the AI left blocks unlabelled; the heuristic labelled them
    "truncated",  # more than MAX_LLM_BLOCKS text blocks: the rest used the heuristic
    "no_headings",  # no title or heading found: one section holds the whole document
    "sections_capped",  # more than MAX_SECTIONS headings: later ones did not start a section
)

MAX_BLOCKS = 20_000
MAX_EXCERPT_CHARS = 300
LLM_EXCERPT_CHARS = 160
MAX_STYLE_CHARS = 100
MAX_TITLE_CHARS = 200
MAX_LEVEL = 9
MAX_SECTIONS = 2000
MAX_LLM_BLOCKS = 4000
BATCH_BLOCKS = 120
BATCH_CHARS = 12_000
MAX_BLOCK_LENGTH = 10_000_000


@dataclass(frozen=True)
class Block:
    """One top-level body item. ``text`` is an excerpt (first ``MAX_EXCERPT_CHARS`` characters,
    whitespace collapsed; a table gives its first cells joined by " | "); ``length`` is the full
    character count. ``level`` is the outline level from ``w:outlineLvl`` or the style (1-9, 0
    none). ``size`` is the dominant run size in pt (``None`` when unknown), ``font`` the dominant
    explicit family (``""`` when inherited), ``bold``/``mono`` true when most characters are."""

    id: str
    kind: str
    text: str
    style: str = ""
    level: int = 0
    length: int = 0
    bold: bool = False
    size: float | None = None
    list: bool = False
    mono: bool = False
    font: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "style": self.style,
            "level": self.level,
            "length": self.length,
            "bold": self.bold,
            "size": self.size,
            "list": self.list,
            "mono": self.mono,
            "font": self.font,
        }

    @classmethod
    def from_json(cls, value: Any, path: str = "block") -> Block:
        keys = ("id", "kind", "text", "style", "level", "length", "bold", "size", "list", "mono")
        data = jc.record(value, path, (*keys, "font"))
        size = data["size"]
        return cls(
            id=jc.required(data["id"], f"{path}.id", 6, BLOCK_ID),
            kind=jc.choice(data["kind"], f"{path}.kind", BLOCK_KINDS),
            text=jc.text(data["text"], f"{path}.text", MAX_EXCERPT_CHARS),
            style=jc.text(data["style"], f"{path}.style", MAX_STYLE_CHARS),
            level=jc.count(data["level"], f"{path}.level", MAX_LEVEL),
            length=jc.count(data["length"], f"{path}.length", MAX_BLOCK_LENGTH),
            bold=jc.flag(data["bold"], f"{path}.bold"),
            size=None if size is None else jc.number(size, f"{path}.size", 1.0, 2 * SIZE_MAX),
            list=jc.flag(data["list"], f"{path}.list"),
            mono=jc.flag(data["mono"], f"{path}.mono"),
            font=clean_family(data["font"], f"{path}.font"),
        )

    def llm_line(self, masked_text: str) -> dict[str, Any]:
        """The JSON object sent to the model for this block (``masked_text`` is the excerpt after
        ``Masker.mask_plain``, cut to ``LLM_EXCERPT_CHARS``). No font name is sent."""
        return {
            "id": self.id,
            "kind": self.kind,
            "style": self.style,
            "level": self.level,
            "bold": self.bold,
            "size": self.size,
            "list": self.list,
            "mono": self.mono,
            "len": self.length,
            "text": masked_text[:LLM_EXCERPT_CHARS],
        }


@dataclass(frozen=True)
class BlockLabel:
    """The type of one block and who decided it (``ai``, ``heuristic`` or ``style``)."""

    id: str
    type: str
    source: str

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "type": self.type, "source": self.source}

    @classmethod
    def from_json(cls, value: Any, path: str = "label") -> BlockLabel:
        data = jc.record(value, path, ("id", "type", "source"))
        return cls(
            id=jc.required(data["id"], f"{path}.id", 6, BLOCK_ID),
            type=jc.choice(data["type"], f"{path}.type", BLOCK_TYPES),
            source=jc.choice(data["source"], f"{path}.source", SOURCES),
        )


@dataclass(frozen=True)
class Section:
    """A run of consecutive blocks opened by a title/heading block (or the leading blocks before
    the first heading, ``heading_block == ""``). ``level`` is 0 for those and for ``title``,
    1-4 for headings. ``type_counts`` (block type -> count) feeds the UI chips."""

    id: str
    title: str
    level: int
    heading_block: str
    block_ids: tuple[str, ...]
    type_counts: tuple[tuple[str, int], ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "level": self.level,
            "heading_block": self.heading_block,
            "block_ids": list(self.block_ids),
            "type_counts": dict(self.type_counts),
        }


def _title(text: str) -> str:
    return " ".join(text.split())[:MAX_TITLE_CHARS]


def _section(index: int, members: list[tuple[Block, BlockLabel]]) -> Section:
    first, label = members[0]
    heading = label.type in HEADING_TYPES
    counts = Counter(item.type for _, item in members)
    return Section(
        id=f"s{index}",
        title=_title(first.text) if heading else "",
        level=heading_level(label.type) if heading else 0,
        heading_block=first.id if heading else "",
        block_ids=tuple(block.id for block, _ in members),
        type_counts=tuple((name, counts[name]) for name in BLOCK_TYPES if counts[name]),
    )


def build_sections(
    blocks: Sequence[Block], labels: Sequence[BlockLabel]
) -> tuple[tuple[Section, ...], bool]:
    """Derive the sections from the labels: every ``title``/``heading_n`` block opens a section
    (up to ``MAX_SECTIONS``; later headings stay inside the last section). Returns the sections
    and whether the cap was hit. ``labels`` must align with ``blocks`` (same ids, same order)."""
    if len(blocks) != len(labels) or any(
        b.id != lab.id for b, lab in zip(blocks, labels, strict=True)
    ):
        raise jc.fail("labels")
    groups: list[list[tuple[Block, BlockLabel]]] = []
    capped = False
    for block, label in zip(blocks, labels, strict=True):
        opens = label.type in HEADING_TYPES
        if opens and len(groups) >= MAX_SECTIONS:
            capped, opens = True, False
        if opens or not groups:
            groups.append([])
        groups[-1].append((block, label))
    return tuple(_section(i, group) for i, group in enumerate(groups, start=1)), capped


@dataclass(frozen=True)
class DocumentSections:
    """The result of detection for one DOCX (``doc_sha256`` is the hash of the DOCX analysed,
    the converted one for a PDF). ``model`` names the LLM used ("" for the heuristic)."""

    doc_sha256: str
    method: str
    model: str
    blocks: tuple[Block, ...]
    labels: tuple[BlockLabel, ...]
    sections: tuple[Section, ...]
    warnings: tuple[str, ...] = ()

    @classmethod
    def build(
        cls,
        *,
        doc_sha256: str,
        model: str,
        blocks: Sequence[Block],
        labels: Sequence[BlockLabel],
        warnings: Iterable[str] = (),
    ) -> DocumentSections:
        """Validate, derive the sections and the method (from the label sources)."""
        if len(blocks) > MAX_BLOCKS:
            raise jc.fail("blocks")
        sections, capped = build_sections(blocks, labels)
        extra = ["sections_capped"] if capped else []
        if not any(label.type in HEADING_TYPES for label in labels):
            extra.append("no_headings")
        found = tuple(dict.fromkeys([*warnings, *extra]))
        if any(code not in WARNINGS for code in found):
            raise jc.fail("warnings")
        sources = {label.source for label in labels if label.source != "style"}
        method = "mixed" if len(sources) > 1 else ("ai" if sources == {"ai"} else "heuristic")
        return cls(
            doc_sha256=jc.required(doc_sha256, "doc_sha256", 64, jc.SHA256),
            method=method,
            model=jc.text(model, "model", 100, pattern=MODEL_NAME),
            blocks=tuple(blocks),
            labels=tuple(labels),
            sections=sections,
            warnings=found,
        )

    def assignments(self) -> dict[str, tuple[str, str]]:
        """block id -> (section id, block type), the input of the typography engine
        (wrap the values in ``docx_typography.model.BlockAssignment``)."""
        types = {label.id: label.type for label in self.labels}
        return {
            block_id: (section.id, types[block_id])
            for section in self.sections
            for block_id in section.block_ids
        }

    def section_ids(self) -> tuple[str, ...]:
        return tuple(section.id for section in self.sections)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": SECTIONS_VERSION,
            "doc_sha256": self.doc_sha256,
            "method": self.method,
            "model": self.model,
            "warnings": list(self.warnings),
            "blocks": [block.to_json() for block in self.blocks],
            "labels": [label.to_json() for label in self.labels],
            "sections": [section.to_json() for section in self.sections],
        }

    @classmethod
    def from_json(cls, value: Any, path: str = "sections") -> DocumentSections:
        keys = ("version", "doc_sha256", "method", "model", "warnings", "blocks", "labels")
        data = jc.record(value, path, (*keys, "sections"))
        if data["version"] != SECTIONS_VERSION or isinstance(data["version"], bool):
            raise jc.fail(f"{path}.version")
        blocks = jc.items(data["blocks"], f"{path}.blocks", MAX_BLOCKS, Block.from_json)
        labels = jc.items(data["labels"], f"{path}.labels", MAX_BLOCKS, BlockLabel.from_json)
        jc.unique((block.id for block in blocks), f"{path}.blocks")
        warnings = jc.items(
            data["warnings"],
            f"{path}.warnings",
            len(WARNINGS),
            lambda v, p: jc.choice(v, p, WARNINGS),
        )
        built = cls.build(
            doc_sha256=data["doc_sha256"],
            model=data["model"],
            blocks=blocks,
            labels=labels,
            warnings=warnings,
        )
        if data["method"] != built.method or data["sections"] != built.to_json()["sections"]:
            raise jc.fail(f"{path}.sections")
        return built


def label_tool(name: str, max_items: int = BATCH_BLOCKS) -> dict[str, Any]:
    """The forced-function tool for one batch. ``name`` must be ``prompts.TOOL_NAME``."""
    item = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "pattern": "^b[0-9]{5}$"},
            "type": {"type": "string", "enum": list(BLOCK_TYPES)},
        },
        "required": ["id", "type"],
        "additionalProperties": False,
    }
    labels = {"type": "array", "maxItems": max_items, "items": item}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Submit exactly one block type for every block id you were given.",
            "parameters": {
                "type": "object",
                "properties": {"labels": labels},
                "required": ["labels"],
                "additionalProperties": False,
            },
        },
    }


def parse_label_answer(value: Any, sent_ids: Sequence[str]) -> dict[str, BlockLabel]:
    """Strictly validate one batch answer ``{"labels": [{"id", "type"}, ...]}``.

    Refused as a whole (:class:`LabelAnswerRejected`): not that shape, more entries than ids
    sent, an id not sent in this batch, an id twice, a type outside ``BLOCK_TYPES``. Missing ids
    are allowed: the caller repairs coverage with :func:`merge_labels`."""
    sent = set(sent_ids)
    try:
        data = jc.record(value, "answer", ("labels",))
        entries = data["labels"]
        if not isinstance(entries, list) or len(entries) > len(sent):
            raise jc.fail("answer.labels")
        found: dict[str, BlockLabel] = {}
        for index, entry in enumerate(entries):
            item = jc.record(entry, f"answer.labels[{index}]", ("id", "type"))
            block_id = jc.choice(item["id"], f"answer.labels[{index}].id", sent)
            if block_id in found:
                raise jc.fail(f"answer.labels[{index}].id")
            kind = jc.choice(item["type"], f"answer.labels[{index}].type", BLOCK_TYPES)
            found[block_id] = BlockLabel(id=block_id, type=kind, source="ai")
    except ValueError as exc:
        raise LabelAnswerRejected() from exc
    return found


def merge_labels(
    blocks: Sequence[Block], primary: Mapping[str, BlockLabel], fallback: Sequence[BlockLabel]
) -> tuple[tuple[BlockLabel, ...], int]:
    """Coverage repair: one label per block, in block order, ``primary`` (AI) first, else the
    aligned ``fallback`` (heuristic) label. Returns the labels and the number of blocks that
    ``primary`` did not cover (the caller decides whether that means ``partial_coverage``)."""
    if len(fallback) != len(blocks) or any(
        b.id != f.id for b, f in zip(blocks, fallback, strict=True)
    ):
        raise jc.fail("fallback")
    merged = tuple(
        primary.get(block.id, backup) for block, backup in zip(blocks, fallback, strict=True)
    )
    missing = sum(1 for block in blocks if block.id not in primary)
    return merged, missing


__all__ = [
    "BATCH_BLOCKS",
    "BATCH_CHARS",
    "BLOCK_ID",
    "BLOCK_KINDS",
    "LLM_EXCERPT_CHARS",
    "MAX_BLOCKS",
    "MAX_EXCERPT_CHARS",
    "MAX_LLM_BLOCKS",
    "MAX_SECTIONS",
    "MAX_TITLE_CHARS",
    "METHODS",
    "SECTION_ID",
    "SOURCES",
    "WARNINGS",
    "Block",
    "BlockLabel",
    "DocumentSections",
    "Section",
    "build_sections",
    "label_tool",
    "merge_labels",
    "parse_label_answer",
]
