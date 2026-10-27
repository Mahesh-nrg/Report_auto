"""Detect sections: AI labels for every text block, the heuristic as fallback and repair.

Contract: EXPORT_DESIGN.md (h). Excerpts and style names are masked before they leave the
process; an answer is only ids and closed-enum types, validated strictly, and any placeholder
left in it rejects the batch. Nothing here changes the document.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..llm import LlmAuthError
from .errors import ExportCancelled, LabelAnswerRejected
from .heuristic import heuristic_labels
from .model import (
    BATCH_BLOCKS,
    BATCH_CHARS,
    MAX_LLM_BLOCKS,
    MODEL_NAME,
    Block,
    BlockLabel,
    DocumentSections,
    label_tool,
    merge_labels,
    parse_label_answer,
)
from .prompts import SYSTEM_PROMPT, TOOL_NAME, batch_user, json_line
from .reader import read_document

log = logging.getLogger(__name__)

OVERLAP = 2  # context lines repeated from the previous batch (never labelled)
DISAGREE_MIN = 4  # style-certain blocks needed before the disagreement rule applies
DISAGREE_SHARE = 0.5  # the AI contradicting more than this share of them rejects the batch
TABLE_TYPES = frozenset(("table", "toc", "code", "other"))


@dataclass(frozen=True)
class Batch:
    ids: tuple[str, ...]
    lines: tuple[dict[str, Any], ...]
    context: frozenset[str]


@dataclass(frozen=True)
class _Outcome:
    labels: dict[str, BlockLabel]
    warnings: tuple[str, ...]


def _noop_progress(done: int, total: int) -> None:
    """Default progress callback."""


def _line(block: Block, masker: Any) -> dict[str, Any]:
    line = block.llm_line(masker.mask_plain(block.text))
    return {**line, "style": masker.mask_plain(block.style)}


def make_batches(blocks: Sequence[Block], masker: Any) -> tuple[Batch, ...]:
    """Split the text blocks into batches of at most BATCH_BLOCKS blocks / BATCH_CHARS chars,
    each opened by up to OVERLAP context lines from the end of the previous one."""
    batches: list[Batch] = []
    ids: list[str] = []
    lines: list[dict[str, Any]] = []
    size = 0
    for block in blocks:
        line = _line(block, masker)
        cost = len(json_line(line))
        if ids and (len(ids) >= BATCH_BLOCKS or size + cost > BATCH_CHARS):
            batches.append(_close(batches, ids, lines))
            ids, lines, size = [], [], 0
        ids.append(block.id)
        lines.append(line)
        size += cost
    if ids:
        batches.append(_close(batches, ids, lines))
    return tuple(batches)


def _close(previous: list[Batch], ids: list[str], lines: list[dict[str, Any]]) -> Batch:
    tail = previous[-1].lines[-OVERLAP:] if previous else ()
    context = tuple({**line, "context": True} for line in tail if not line.get("context"))
    return Batch(tuple(ids), (*context, *lines), frozenset(line["id"] for line in context))


def _answer(raw: Any) -> Any:
    return raw[0] if isinstance(raw, tuple) and raw else raw


def _drop_context(value: Any, context: frozenset[str]) -> Any:
    if not context or not isinstance(value, dict) or not isinstance(value.get("labels"), list):
        return value
    kept = [
        item
        for item in value["labels"]
        if not (isinstance(item, dict) and item.get("id") in context)
    ]
    return {**value, "labels": kept}


def _check_disagreement(found: Mapping[str, BlockLabel], heuristic: Mapping[str, BlockLabel]):
    certain = [key for key in found if heuristic[key].source == "style"]
    wrong = sum(1 for key in certain if found[key].type != heuristic[key].type)
    if len(certain) >= DISAGREE_MIN and wrong > DISAGREE_SHARE * len(certain):
        raise LabelAnswerRejected()


def _keep_tables(
    found: dict[str, BlockLabel], kinds: Mapping[str, str], heuristic: Mapping[str, BlockLabel]
) -> dict[str, BlockLabel]:
    """A whole table can only be table/toc/code/other: anything else keeps the heuristic."""
    return {
        key: (heuristic[key] if kinds[key] == "table" and item.type not in TABLE_TYPES else item)
        for key, item in found.items()
    }


def ask_batch(
    llm: Any, batch: Batch, masker: Any, heuristic: Mapping[str, BlockLabel], kinds: Mapping
) -> dict[str, BlockLabel]:
    """One validated batch answer. Raises LabelAnswerRejected; LLM errors pass through."""
    tool = label_tool(TOOL_NAME, len(batch.ids))
    value = _answer(llm.call_tool(SYSTEM_PROMPT, batch_user(batch.lines), tool))
    try:
        if masker.leftovers(value):
            raise LabelAnswerRejected()
    except ValueError:
        raise LabelAnswerRejected() from None
    found = parse_label_answer(_drop_context(value, batch.context), batch.ids)
    _check_disagreement(found, heuristic)
    return _keep_tables(found, kinds, heuristic)


STOP_WARNINGS = frozenset(("llm_key", "llm_failed"))


def _one_batch(llm: Any, batch: Batch, masker: Any, context: tuple) -> tuple[dict, str | None]:
    """The labels of one batch and the warning it raised (``None`` when clean)."""
    heuristic, kinds = context
    try:
        found = ask_batch(llm, batch, masker, heuristic, kinds)
    except LabelAnswerRejected:
        return {}, "answer_rejected"
    except LlmAuthError:
        return {}, "llm_key"
    except Exception as exc:  # noqa: BLE001 - any model failure falls back to the heuristic
        log.warning("section labelling failed (%s)", type(exc).__name__)
        return {}, "llm_failed"
    return found, ("partial_coverage" if len(found) < len(batch.ids) else None)


def _run_batches(
    llm: Any,
    batches: Sequence[Batch],
    masker: Any,
    context: tuple[Mapping[str, BlockLabel], Mapping[str, str]],
    hooks: tuple[Callable[[], bool], Callable[[int, int], None]],
) -> _Outcome:
    cancelled, progress = hooks
    labels: dict[str, BlockLabel] = {}
    warnings: list[str] = []
    for index, batch in enumerate(batches):
        if cancelled():
            raise ExportCancelled()
        found, warning = _one_batch(llm, batch, masker, context)
        labels.update(found)
        if warning is not None:
            warnings.append(warning)
        if warning in STOP_WARNINGS:
            break
        progress(index + 1, len(batches))
    return _Outcome(labels, tuple(warnings))


def _model_name(llm: Any, model: str) -> str:
    name = str(getattr(llm, "last_model", "") or model or "")[:100]
    return name if MODEL_NAME.fullmatch(name) else ""


def _ai_labels(
    blocks: Sequence[Block],
    heuristic: Sequence[BlockLabel],
    llm: Any,
    masker: Any,
    hooks: tuple[Callable[[], bool], Callable[[int, int], None]],
) -> tuple[dict[str, BlockLabel], list[str]]:
    text_blocks = [block for block in blocks if block.text]
    warnings = ["truncated"] if len(text_blocks) > MAX_LLM_BLOCKS else []
    sent = text_blocks[:MAX_LLM_BLOCKS]
    by_id = {label.id: label for label in heuristic}
    kinds = {block.id: block.kind for block in blocks}
    batches = make_batches(sent, masker)
    outcome = _run_batches(llm, batches, masker, (by_id, kinds), hooks)
    warnings.extend(outcome.warnings)
    return outcome.labels, warnings


def detect_sections(
    data: bytes,
    *,
    llm: Any | None,
    masker: Any,
    model: str = "",
    cancelled: Callable[[], bool] = lambda: False,
    progress: Callable[[int, int], None] = _noop_progress,
    no_llm_warning: str = "llm_unavailable",
) -> DocumentSections:
    """Read the DOCX, label every block and derive the sections. ``llm=None`` uses the heuristic
    only and records ``no_llm_warning`` (``llm_unavailable`` or ``ai_disabled``)."""
    read = read_document(data)
    heuristic = heuristic_labels(read.blocks, read.figures)
    if cancelled():
        raise ExportCancelled()
    primary: dict[str, BlockLabel] = {}
    warnings: list[str] = [no_llm_warning] if llm is None else []
    if llm is not None:
        primary, found = _ai_labels(read.blocks, heuristic, llm, masker, (cancelled, progress))
        warnings.extend(found)
    labels, _missing = merge_labels(read.blocks, primary, heuristic)
    return DocumentSections.build(
        doc_sha256=hashlib.sha256(data).hexdigest(),
        model=_model_name(llm, model) if primary else "",
        blocks=read.blocks,
        labels=labels,
        warnings=warnings,
    )
