"""One chunk: ask the model, then validate every returned paragraph and addition."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pwndoc_ai.llm import LlmAuthError, LlmError

from . import limits
from .errors import DocxError
from .maskio import MaskAdapter
from .model import Change
from .plan import Chunk, split_chunk
from .prompts import build_user_message
from .textclean import clean_model_text
from .validate import DEEP, Verdict, validate_addition, validate_paragraph

log = logging.getLogger(__name__)
CUT_OFF = "cut off"
NOT_ADDABLE = "a paragraph cannot be added after this one without a trace in Word"
UNRESTORED = "the answer contained an unrestored stand-in or placeholder"


@dataclass(frozen=True)
class ChunkOutcome:
    """What one chunk produced. ``edits`` and ``additions`` are the accepted changes only."""

    changes: tuple[Change, ...] = ()
    edits: tuple[tuple[str, str], ...] = ()  # (unit id, new text)
    additions: tuple[tuple[str, str], ...] = ()  # (after unit id, text)
    ignored: int = 0
    answered: int = 0  # paragraphs whose chunk got a usable answer
    tokens: int = 0
    models: tuple[str, ...] = ()
    failed: bool = False

    def merge(self, other: ChunkOutcome) -> ChunkOutcome:
        return ChunkOutcome(
            self.changes + other.changes,
            self.edits + other.edits,
            self.additions + other.additions,
            self.ignored + other.ignored,
            self.answered + other.answered,
            self.tokens + other.tokens,
            tuple(dict.fromkeys(self.models + other.models)),
            self.failed or other.failed,
        )


@dataclass
class _Tally:
    changes: list[Change] = field(default_factory=list)
    edits: list[tuple[str, str]] = field(default_factory=list)
    additions: list[tuple[str, str]] = field(default_factory=list)
    ignored: int = 0


class ChunkRunner:
    """Runs chunks against one model with one masker, mode and system prompt."""

    def __init__(self, llm: Any, adapter: MaskAdapter, mode: str, system: str, tool: dict) -> None:
        self._llm, self._adapter, self._mode = llm, adapter, mode
        self._system, self._tool = system, tool

    def run(self, chunk: Chunk, research: str) -> ChunkOutcome:
        """Ask once; on a cut-off answer split the chunk in half and ask once for each half."""
        return self._ask(chunk, research, may_split=True)

    def _ask(self, chunk: Chunk, research: str, *, may_split: bool) -> ChunkOutcome:
        try:
            masked = {unit.id: self._adapter.mask(unit.text) for unit in chunk.units}
            title = self._adapter.mask(chunk.title)
        except ValueError as exc:  # a stand-in collision: this chunk is not sent at all
            log.warning("a chunk could not be masked (%s); skipping it", type(exc).__name__)
            return ChunkOutcome(failed=True)
        user = build_user_message(title, list(masked.items()), research)
        try:
            args, usage = self._llm.call_tool(self._system, user, self._tool)
        except LlmAuthError:
            raise DocxError("The AI provider rejected the configured credentials.") from None
        except LlmError as exc:
            if may_split and len(chunk.units) > 1 and CUT_OFF in str(exc):
                first, second = split_chunk(chunk)
                return self._ask(first, research, may_split=False).merge(
                    self._ask(second, research, may_split=False)
                )
            log.warning("a chunk failed (%s); skipping it", type(exc).__name__)
            return ChunkOutcome(failed=True)
        models = (str(getattr(self._llm, "last_model", "") or ""),)
        tokens = int(getattr(usage, "total", 0) or 0)
        if not isinstance(args, dict) or not isinstance(args.get("paragraphs"), list):
            log.warning("a chunk answer was unusable; skipping it")
            return ChunkOutcome(tokens=tokens, models=models, failed=True)
        try:
            tally = self._judge_all(chunk, masked, args, research)
        except Exception as exc:  # noqa: BLE001 - a bad answer costs this chunk, not the whole run
            log.warning("a chunk answer could not be judged (%s); skipping it", type(exc).__name__)
            return ChunkOutcome(tokens=tokens, models=tuple(m for m in models if m), failed=True)
        return ChunkOutcome(
            tuple(tally.changes), tuple(tally.edits), tuple(tally.additions), tally.ignored,
            len(chunk.units), tokens, tuple(m for m in models if m), False,
        )  # fmt: skip

    def _judge_all(
        self, chunk: Chunk, masked: dict[str, str], args: dict[str, Any], research: str
    ) -> _Tally:
        tally = _Tally()
        by_id = {unit.id: unit for unit in chunk.units}
        seen: set[str] = set()
        for item in args["paragraphs"]:
            unit_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(unit_id, str) or unit_id not in by_id or unit_id in seen:
                tally.ignored += 1
                continue
            if "text" not in item:
                tally.ignored += 1
                continue
            seen.add(unit_id)
            unit = by_id[unit_id]
            verdict = self._judge(unit.text, masked[unit_id], item["text"], research)
            tally.changes.append(
                Change(
                    unit_id,
                    chunk.section_id,
                    unit.text,
                    verdict.text,
                    verdict.status,
                    verdict.reason,
                )
            )
            if verdict.status == "applied":
                tally.edits.append((unit_id, verdict.text))
        self._judge_additions(chunk, by_id, args.get("additions"), research, tally)
        return tally

    def _judge(self, original: str, masked: str, proposed: object, research: str) -> Verdict:
        if not isinstance(proposed, str):
            return Verdict("rejected", "the answer was not text", "")
        restored = self._adapter.unmask(proposed)
        shown = clean_model_text(restored, original)[: limits.MAX_OUTPUT_PARAGRAPH_CHARS]
        if self._adapter.stray(restored, original):
            return Verdict("rejected", UNRESTORED, shown)
        if self._adapter.dropped(masked, restored):
            return Verdict("rejected", "a protected name or address was removed", shown)
        return validate_paragraph(original, restored, mode=self._mode, research=research)

    def _judge_additions(
        self, chunk: Chunk, by_id: dict[str, Any], items: object, research: str, tally: _Tally
    ) -> None:
        if items is None:
            return
        if self._mode != DEEP or not isinstance(items, list):
            tally.ignored += len(items) if isinstance(items, list) else 1
            return
        applied = 0
        for item in items:
            after = item.get("after_id") if isinstance(item, dict) else None
            if not isinstance(after, str) or after not in by_id or "text" not in item:
                tally.ignored += 1
                continue
            if not by_id[after].addable:
                tally.changes.append(
                    Change(after, chunk.section_id, "", "", "rejected", NOT_ADDABLE)
                )
                continue
            verdict = self._judge_addition(by_id[after].text, item["text"], research)
            if verdict.status == "applied" and applied >= limits.MAX_ADDITIONS_PER_CHUNK:
                verdict = Verdict("rejected", "too many additions in one chunk", verdict.text)
            applied += verdict.status == "applied"
            tally.changes.append(
                Change(after, chunk.section_id, "", verdict.text, verdict.status, verdict.reason)
            )
            if verdict.status == "applied":
                tally.additions.append((after, verdict.text))

    def _judge_addition(self, source: str, proposed: object, research: str) -> Verdict:
        if isinstance(proposed, str):
            proposed = self._adapter.unmask(proposed)
            if self._adapter.stray(proposed, source):
                shown = clean_model_text(proposed, source)[: limits.MAX_ADDITION_CHARS]
                return Verdict("rejected", UNRESTORED, shown)
        return validate_addition(proposed, source=source, research=research)
