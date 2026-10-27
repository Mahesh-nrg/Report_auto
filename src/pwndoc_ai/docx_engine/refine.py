"""refine_docx: go through an existing DOCX with the model and rewrite its text in place."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from pwndoc_ai.masking import Masker, NullMasker

from . import limits
from .chunk_run import ChunkOutcome, ChunkRunner
from .errors import DocxError, RefineCancelled
from .inspection import LoadedDocument, load_document
from .maskio import MaskAdapter
from .model import Inspection, Progress, RefineRequest, RefineResult
from .outputs import OutputsCancelled, build_outputs
from .plan import Chunk, plan_chunks, section_body, select_sections
from .prompts import build_system_prompt, build_tool
from .research import EXCERPT_SOURCE_CHARS, ResearchSource

log = logging.getLogger(__name__)
MODES = ("light", "deep")
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)$")
MAX_AUTHOR = 80
MAX_PROGRESS_TITLE = 120
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MASK_FAILED = "Could not prepare masking for the document."


def _default_masker(request: RefineRequest, settings: Any) -> Masker | NullMasker:
    if settings is not None and not settings.mask_sensitive and not request.client_terms:
        return NullMasker()
    return Masker([("TERM", term) for term in request.client_terms])


def _build_masker(
    request: RefineRequest, factory: Callable[[tuple[str, ...]], Any] | None, settings: Any
) -> MaskAdapter:
    terms = tuple(t for t in request.client_terms if isinstance(t, str))
    try:
        masker = factory(terms) if factory else _default_masker(request, settings)
        return MaskAdapter(masker)
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, never send unmasked text
        log.warning("masking could not be prepared (%s)", type(exc).__name__)
        raise DocxError(MASK_FAILED) from None


def _prepare_masking(adapter: MaskAdapter, inspection: Inspection) -> None:
    """Register every paragraph and heading of the document before the first chunk is masked,
    so no stand-in can equal text that is already in the document (fail closed)."""
    corpus = [unit.text for unit in inspection.units]
    corpus += [section.title for section in inspection.sections]
    try:
        adapter.prepare(corpus)
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, never send unmasked text
        log.warning("masking could not be prepared (%s)", type(exc).__name__)
        raise DocxError(MASK_FAILED) from None


def _check_request(request: RefineRequest, timestamp: str, author: str) -> str:
    if request.mode not in MODES:
        raise DocxError("Unknown refine mode.")
    if not isinstance(timestamp, str) or not TIMESTAMP.match(timestamp):
        raise DocxError("The revision timestamp is not valid.")
    return CONTROL.sub("", author or "AI refine")[:MAX_AUTHOR] or "AI refine"


def _protected_in_scope(inspection: Inspection, section_ids: tuple[str, ...] | None) -> int:
    count = 0
    for section in select_sections(inspection, section_ids):
        units = inspection.units[section.first_unit : section.last_unit + 1]
        count += sum(1 for unit in units if unit.protected)
    return count


@dataclass(frozen=True)
class _State:
    """Everything a (possibly partial) run knows; replaced, never mutated, after each chunk."""

    loaded: LoadedDocument
    total_chunks: int
    protected: int
    outcome: ChunkOutcome = ChunkOutcome()
    done_chunks: int = 0
    failed_chunks: int = 0

    def after(self, outcome: ChunkOutcome) -> _State:
        return replace(
            self,
            outcome=self.outcome.merge(outcome),
            done_chunks=self.done_chunks + 1,
            failed_chunks=self.failed_chunks + int(outcome.failed),
        )

    def progress(self, title: str) -> Progress:
        changes = self.outcome.changes
        return Progress(
            total_chunks=self.total_chunks,
            done_chunks=self.done_chunks,
            changed=sum(1 for c in changes if c.status == "applied"),
            rejected=sum(1 for c in changes if c.status == "rejected"),
            protected=self.protected,
            current=title[:MAX_PROGRESS_TITLE],
        )


def _stats(state: _State, research_failed: int) -> dict[str, object]:
    out = state.outcome
    applied = sum(1 for c in out.changes if c.status == "applied")
    rejected_edits = sum(1 for c in out.changes if c.status == "rejected" and c.before)
    return {
        "changed": applied,
        "unchanged": max(0, out.answered - len(out.edits) - rejected_edits),
        "rejected": sum(1 for c in out.changes if c.status == "rejected"),
        "protected": state.protected,
        "chunks": state.total_chunks,
        "failed_chunks": state.failed_chunks,
        "model_names": list(out.models),
        "tokens": out.tokens,
        "ignored": out.ignored,
        "additions": len(out.additions),
        "research_failed": research_failed,
    }


def _result(
    state: _State,
    author: str,
    timestamp: str,
    research: ResearchSource | None,
    cancel: threading.Event | None = None,
) -> RefineResult:
    out = state.outcome
    stats = _stats(state, research.failed if research else 0)
    try:
        clean, tracked = build_outputs(
            state.loaded, out.edits, out.additions, author=author, timestamp=timestamp,
            cancel=cancel,
        )  # fmt: skip
    except OutputsCancelled:
        original = state.loaded.package.data  # cancelled while building: nothing is applied
        raise RefineCancelled(RefineResult(original, original, out.changes, stats)) from None
    return RefineResult(clean, tracked, out.changes, stats)


def _research_text(research: ResearchSource | None, inspection: Inspection, chunk: Chunk) -> str:
    if research is None:
        return ""
    section = next(s for s in inspection.sections if s.id == chunk.section_id)
    body = section_body(inspection, section, EXCERPT_SOURCE_CHARS)
    return research.for_section(section, body)


def _check_research_scope(chunks: tuple[Chunk, ...]) -> None:
    """Deep mode looks up every section on the web: refuse a scope with too many sections."""
    if len({chunk.section_id for chunk in chunks}) > limits.MAX_RESEARCH_SECTIONS:
        raise DocxError(
            "Deep refine would research too many sections for one run; choose fewer sections "
            "or use light mode."
        )


def _notify(on_progress: Callable[[Progress], None] | None, progress: Progress) -> None:
    if on_progress is None:
        return
    try:
        on_progress(progress)
    except Exception as exc:  # noqa: BLE001 - a broken progress sink must not abort the run
        log.warning("progress callback failed (%s)", type(exc).__name__)


def refine_docx(
    data: bytes,
    request: RefineRequest,
    llm: Any,
    *,
    context: Callable[..., Any] | None = None,
    masker_factory: Callable[[tuple[str, ...]], Any] | None = None,
    on_progress: Callable[[Progress], None] | None = None,
    cancel: threading.Event | None = None,
    author: str = "AI refine",
    timestamp: str,
    settings: Any = None,
) -> RefineResult:
    """Refine the text of an uploaded DOCX in place and return the clean and tracked files.

    Raises DocxError for a bad document, request or masking setup (the model is not called in
    that case) and for rejected credentials. Raises RefineCancelled when ``cancel`` is set
    between chunks; it carries the result of the chunks finished so far. A chunk the model
    cannot answer is skipped and counted in ``stats["failed_chunks"]``.
    """
    author = _check_request(request, timestamp, author)
    loaded = load_document(data)
    inspection = loaded.inspection
    chunks = plan_chunks(inspection, request.section_ids)
    adapter = _build_masker(request, masker_factory, settings)
    _prepare_masking(adapter, inspection)
    house = getattr(settings, "extra_instructions", "") or ""
    runner = ChunkRunner(
        llm, adapter, request.mode,
        build_system_prompt(request.mode, request.language_hint, house), build_tool(request.mode),
    )  # fmt: skip
    research = ResearchSource(context, adapter) if request.mode == "deep" and context else None
    if research is not None:
        _check_research_scope(chunks)
    state = _State(loaded, len(chunks), _protected_in_scope(inspection, request.section_ids))
    for chunk in chunks:
        _notify(on_progress, state.progress(chunk.title))
        if cancel is not None and cancel.is_set():
            raise RefineCancelled(_result(state, author, timestamp, research))
        text = _research_text(research, inspection, chunk)
        state = state.after(runner.run(chunk, text))
        _notify(on_progress, state.progress(chunk.title))
    return _result(state, author, timestamp, research, cancel)
