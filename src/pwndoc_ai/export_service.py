"""The Export tab service: ingest an upload, analyse it in the background, keep the font plan,
render the DOCX with the plan applied.

Contract: docs/EXPORT_DESIGN.md (c), (g), (h). Every error a user can see is an
``ExportFailure`` (fixed sentence); the log carries exception types only, never text, names or
paths. The engines come from :mod:`export_toolkit` (looked up per call, so tests replace them).
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import export_json as jc
from . import export_toolkit
from .docx_sections import errors as sections_errors
from .docx_sections.model import DocumentSections
from .docx_typography import errors as typography_errors
from .docx_typography.model import (
    MAX_REVISION,
    BlockAssignment,
    TypographyPlan,
    font_uses_from_json,
)
from .export_errors import (
    ExportCancelled,
    ExportConflict,
    ExportFailure,
    ExportInputError,
    ExportNotFound,
    ExportUnavailable,
)
from .export_jobs import ExportJobs, JobHandle
from .export_store import INTERRUPTED, ExportRecord, ExportStore, StagedExport
from .pdf_import import errors as pdf_errors
from .pdf_import.model import ConversionReport, EngineInfo, engine_choice

log = logging.getLogger(__name__)

MAX_TERMS = 50
MAX_TERM_CHARS = 200
CONVERT_SECONDS = 180.0
RENDER_SECONDS = 20.0
ANALYSE_KEYS = ("engine", "use_ai", "client_terms")
PLAN_KEYS = ("plan", "rev")
STEP_OF_STATE = {"converting": "converting", "analysing": "analysing"}
INTERRUPTED_MESSAGE = "the gateway restarted while this file was being analysed; analyse it again"
ERROR_CLASSES: dict[str, type[ExportFailure]] = {
    cls.__name__: cls
    for module in (pdf_errors, sections_errors, typography_errors)
    for cls in vars(module).values()
    if isinstance(cls, type) and issubclass(cls, ExportFailure)
}


def error_code(exc: ExportFailure) -> str:
    """``Class.code`` as stored in the record (both parts are from closed sets)."""
    return f"{type(exc).__name__}.{exc.code}"


def error_message(code: str) -> str:
    """The fixed sentence of a stored ``error_code`` ("" for none)."""
    if not code:
        return ""
    if code == INTERRUPTED:
        return INTERRUPTED_MESSAGE
    name, _, short = code.partition(".")
    cls = ERROR_CLASSES.get(name)
    return str(cls(short)) if cls is not None else str(ExportFailure())


def _register_error_classes() -> None:
    from . import export_errors

    for value in vars(export_errors).values():
        if isinstance(value, type) and issubclass(value, ExportFailure):
            ERROR_CLASSES.setdefault(value.__name__, value)


_register_error_classes()


@dataclass(frozen=True)
class AnalyseOptions:
    engine: str = "auto"
    use_ai: bool = True
    client_terms: tuple[str, ...] = ()


def parse_analyse(body: Any) -> AnalyseOptions:
    """The strict ``POST /files/{id}/analyse`` body."""
    data = jc.record(body, "body", (), ANALYSE_KEYS)
    terms = jc.strings(data.get("client_terms", []), "client_terms", MAX_TERMS, MAX_TERM_CHARS)
    return AnalyseOptions(
        engine=engine_choice(data.get("engine", "auto"), "engine"),
        use_ai=jc.flag(data.get("use_ai", True), "use_ai"),
        client_terms=tuple(dict.fromkeys(term.strip() for term in terms if term.strip())),
    )


def parse_plan_body(body: Any) -> tuple[TypographyPlan, int]:
    """The strict ``PUT /files/{id}/plan`` body."""
    data = jc.record(body, "body", PLAN_KEYS)
    rev = jc.count(data["rev"], "rev", MAX_REVISION)
    return TypographyPlan.from_json(data["plan"], "plan"), rev


@dataclass(frozen=True)
class Rendered:
    """The typographed DOCX and its ``X-Typography*`` headers."""

    data: bytes
    headers: dict[str, str]
    families: tuple[str, ...]


class ExportService:
    """One per gateway. ``settings`` and ``llm_factory`` are late-bound (read per analysis)."""

    def __init__(
        self,
        store: ExportStore,
        *,
        llm_factory: Callable[[Any], Any],
        settings: Callable[[], Any],
        data_dir: Path,
        jobs: ExportJobs | None = None,
    ):
        self.store = store
        self.jobs = jobs or ExportJobs()
        self._llm_factory = llm_factory
        self._settings = settings
        self.data_dir = Path(data_dir)

    @staticmethod
    def toolkit() -> export_toolkit.Toolkit:
        return export_toolkit.default_toolkit()

    # -- catalog and engines ----------------------------------------------------------------
    def fonts(self) -> Any:
        return self.toolkit().font_catalog(self.data_dir)

    def engines(self) -> tuple[EngineInfo, ...]:
        try:
            return tuple(self.toolkit().engines())
        except ImportError:
            return (
                EngineInfo("structure", export_toolkit.pdf_tools_present(), "tools_missing"),
                EngineInfo("pdf2docx", False, "not_installed"),
            )

    def _engine_available(self, name: str) -> bool:
        return any(info.name == name and info.available for info in self.engines())

    # -- upload -----------------------------------------------------------------------------
    def ingest(self, staged: StagedExport, engine: str) -> ExportRecord:
        """Check a staged upload (PDF: tools present, then the bounded precheck) and store it."""
        try:
            chosen = engine_choice(engine, "engine")
            if staged.kind == "pdf":
                export_toolkit.require(not export_toolkit.pdf_tools_present(), "pdf_tools")
                self._precheck(staged.path)
            return self.store.commit(staged, engine=chosen)
        finally:
            staged.discard()

    def _precheck(self, path: Path) -> None:
        try:
            self.toolkit().precheck_pdf(path, deadline=export_toolkit.PRECHECK_SECONDS)
        except ExportFailure:
            raise
        except ImportError as exc:
            raise ExportUnavailable("pdf_tools") from exc
        except Exception as exc:  # noqa: BLE001 - hostile input: never a 500 or a detail
            log.warning("export PDF precheck failed (%s)", type(exc).__name__)
            raise pdf_errors.PdfRefused("unreadable") from exc

    # -- reading ----------------------------------------------------------------------------
    @staticmethod
    def item(record: ExportRecord) -> dict[str, Any]:
        return {**record.to_json(), "error": error_message(record.error_code)}

    def listing(self, owner: str) -> dict[str, Any]:
        records = self.store.list(owner)
        return {
            "files": [self.item(record) for record in records],
            "quota": self.store.quota(owner),
        }

    def status(self, owner: str, file_id: str) -> dict[str, Any]:
        record = self.store.get(owner, file_id)
        job = self.jobs.get(owner, file_id)
        live = job.snapshot() if job is not None and not job.finished.is_set() else None
        step = STEP_OF_STATE.get(record.state, "")
        return {
            "state": record.state,
            "done": live["done"] if live else record.done,
            "total": live["total"] if live else record.total,
            "step": (live["step"] or step) if live else step,
            "error": error_message(record.error_code),
        }

    def ready_record(self, owner: str, file_id: str) -> ExportRecord:
        record = self.store.get(owner, file_id)
        if record.running or self.jobs.is_active(owner, file_id):
            raise ExportConflict("running")
        if record.state != "ready":
            raise ExportConflict("not_ready")
        return record

    def sections(self, owner: str, file_id: str) -> DocumentSections:
        raw = self.store.read_json(owner, file_id, "sections.json")
        try:
            return DocumentSections.from_json(raw)
        except ValueError as exc:
            log.warning("stored sections are invalid (%s)", type(exc).__name__)
            raise ExportNotFound("sections") from exc

    def plan(self, owner: str, file_id: str) -> TypographyPlan:
        try:
            raw = self.store.read_json(owner, file_id, "plan.json")
            return TypographyPlan.from_json(raw)
        except (ExportNotFound, ValueError):
            return TypographyPlan()

    def report(self, owner: str, file_id: str, record: ExportRecord) -> dict[str, Any] | None:
        if record.kind != "pdf":
            return None
        try:
            raw = self.store.read_json(owner, file_id, "report.json")
            return ConversionReport.from_json(raw).to_json()
        except (ExportNotFound, ValueError):
            return None

    def fonts_used(self, owner: str, file_id: str) -> list[dict[str, Any]]:
        try:
            raw = self.store.read_json(owner, file_id, "fonts.json")
            return [use.to_json() for use in font_uses_from_json(raw)]
        except (ExportNotFound, ValueError):
            return []

    def detail(self, owner: str, file_id: str) -> dict[str, Any]:
        record = self.ready_record(owner, file_id)
        return {
            "file": self.item(record),
            "report": self.report(owner, file_id, record),
            "sections": self.sections(owner, file_id).to_json(),
            "plan": self.plan(owner, file_id).to_json(),
            "plan_rev": record.plan_rev,
            "fonts_used": self.fonts_used(owner, file_id),
        }

    # -- plan -------------------------------------------------------------------------------
    def save_plan(self, owner: str, file_id: str, body: Any) -> dict[str, Any]:
        self.ready_record(owner, file_id)
        plan, rev = parse_plan_body(body)
        plan.check_sections(self.sections(owner, file_id).section_ids())
        new_rev = self.store.bump_plan(owner, file_id, rev, plan.to_json())
        if new_rev < 0:
            raise ExportConflict("stale")
        return {"plan": plan.to_json(), "plan_rev": new_rev}

    # -- analysis ---------------------------------------------------------------------------
    def start(self, owner: str, file_id: str, body: Any) -> None:
        record = self.store.get(owner, file_id)  # 404 before a body error
        options = parse_analyse(body)
        if record.kind == "pdf":
            self._check_engine(options.engine)
        if record.running or self.jobs.is_active(owner, file_id):
            raise ExportConflict("running")
        # the state is written before the thread starts: the thread's own writes come later
        self.store.update(owner, file_id, state=self._first_state(record), error_code="")
        try:
            self.jobs.start(owner, file_id, lambda job: self._analyse(job, options))
        except ExportConflict:
            self.store.update(owner, file_id, state=record.state, error_code=record.error_code)
            raise

    @staticmethod
    def _first_state(record: ExportRecord) -> str:
        return "converting" if record.kind == "pdf" else "analysing"

    def _check_engine(self, engine: str) -> None:
        export_toolkit.require(not export_toolkit.pdf_tools_present(), "pdf_tools")
        if engine == "pdf2docx" and not self._engine_available("pdf2docx"):
            raise pdf_errors.PdfEngineUnavailable("pdf2docx")

    def cancel(self, owner: str, file_id: str) -> dict[str, bool]:
        self.store.get(owner, file_id)
        self.jobs.cancel(owner, file_id)
        return {"cancelled": True}

    def delete(self, owner: str, file_id: str) -> None:
        """No meta read: a file whose ``meta.json`` is damaged must stay deletable."""
        if self.jobs.is_active(owner, file_id):
            raise ExportConflict("running")
        self.store.delete(owner, file_id)

    def _analyse(self, job: JobHandle, options: AnalyseOptions) -> None:
        owner, file_id = job.owner, job.file_id
        try:
            record = self.store.update(owner, file_id, engine=options.engine)
            data = self._docx_of(job, record, options.engine)
            self._detect(job, data, options)
            self._finish(job, "ready", "")
        except ExportCancelled:
            self._finish(job, "cancelled", "")
        except ExportFailure as exc:
            self._finish(job, "error", error_code(exc))
        except Exception as exc:  # noqa: BLE001 - hostile input: log the type only
            log.warning("export analysis failed (%s)", type(exc).__name__)
            self._finish(job, "error", error_code(ExportFailure()))

    def _finish(self, job: JobHandle, state: str, code: str) -> None:
        if job.cancelled() and state != "error":
            state = "cancelled"
        snap = job.snapshot()
        try:
            self.store.update(
                job.owner,
                job.file_id,
                state=state,
                error_code=code,
                done=snap["done"],
                total=snap["total"],
            )
        except (ExportNotFound, OSError) as exc:
            log.warning("export record not updated (%s)", type(exc).__name__)

    @staticmethod
    def _check_cancel(job: JobHandle) -> None:
        if job.cancelled():
            raise ExportCancelled()

    def _docx_of(self, job: JobHandle, record: ExportRecord, engine: str) -> bytes:
        """The DOCX to analyse: the upload itself, or the PDF converted with ``engine``."""
        owner, file_id = job.owner, job.file_id
        if record.kind == "docx":
            return self.store.read_bytes(owner, file_id, "source.docx")
        job.set_step("converting")
        self.store.drop(owner, file_id, "converted.docx", "report.json")
        source = self.store.path_of(owner, file_id, "source.pdf")
        work = Path(tempfile.mkdtemp(prefix="export-", dir=self.store.work_dir()))
        try:
            data, report = self.toolkit().convert_pdf(source, work, engine, timeout=CONVERT_SECONDS)
        except ImportError as exc:
            raise ExportUnavailable("pdf_tools") from exc
        finally:
            shutil.rmtree(work, ignore_errors=True)
        self._check_cancel(job)
        self.store.write_bytes(owner, file_id, "converted.docx", data)
        self.store.write_json(owner, file_id, "report.json", report.to_json())
        return data

    def _llm(self, settings: Any, use_ai: bool) -> tuple[Any, str]:
        if not use_ai or not getattr(settings, "llm_configured", False):
            return None, ""
        return self._llm_factory(settings), str(getattr(settings, "llm_model", "") or "")

    def _detect(self, job: JobHandle, data: bytes, options: AnalyseOptions) -> None:
        from .docx_jobs import masker_factory_for

        owner, file_id = job.owner, job.file_id
        job.set_step("analysing")
        self.store.update(owner, file_id, state="analysing")
        settings = self._settings()
        llm, model = self._llm(settings, options.use_ai)
        masker = masker_factory_for(settings)(options.client_terms)
        try:
            found = self.toolkit().detect_sections(
                data,
                llm=llm,
                masker=masker,
                model=model,
                cancelled=job.cancelled,
                progress=job.progress,
            )
        finally:
            _close(llm)
        self._check_cancel(job)
        if not options.use_ai:
            found = _ai_disabled(found)
        self._store_analysis(job, data, found)

    def _store_analysis(self, job: JobHandle, data: bytes, found: DocumentSections) -> None:
        owner, file_id = job.owner, job.file_id
        self.store.write_json(owner, file_id, "sections.json", found.to_json())
        self.store.write_json(owner, file_id, "fonts.json", self._fonts_used_of(data))
        current = self.plan(owner, file_id)
        try:
            current.check_sections(found.section_ids())
        except ExportInputError:
            rev = self.store.get(owner, file_id).plan_rev
            self.store.bump_plan(owner, file_id, rev, TypographyPlan().to_json())

    def _fonts_used_of(self, data: bytes) -> list[dict[str, Any]]:
        try:
            uses = self.toolkit().fonts_used(data)
            return [use.to_json() for use in tuple(uses)[:20]]
        except Exception as exc:  # noqa: BLE001 - informational only
            log.warning("fonts in document not listed (%s)", type(exc).__name__)
            return []

    # -- rendering --------------------------------------------------------------------------
    def analysed_docx(self, owner: str, file_id: str, record: ExportRecord) -> bytes:
        name = "converted.docx" if record.kind == "pdf" else "source.docx"
        return self.store.read_bytes(owner, file_id, name)

    def render(self, owner: str, file_id: str) -> tuple[ExportRecord, Rendered]:
        """The analysed DOCX with the stored plan applied (unchanged bytes for an empty plan)."""
        record = self.ready_record(owner, file_id)
        data = self.analysed_docx(owner, file_id, record)
        found = self.sections(owner, file_id)  # a damaged analysis gives no download
        plan = self.plan(owner, file_id)
        if plan.is_empty:
            return record, Rendered(data, {"X-Typography": "0"}, ())
        assignments = {
            block_id: BlockAssignment(section_id=sid, block_type=kind)
            for block_id, (sid, kind) in found.assignments().items()
        }
        try:
            result = self.toolkit().apply_plan(data, plan, assignments, seconds=RENDER_SECONDS)
        except ExportFailure:
            raise
        except ImportError as exc:
            raise ExportUnavailable("docx") from exc
        except Exception as exc:  # noqa: BLE001 - hostile document: never a 500 or a detail
            log.warning("export typography failed (%s)", type(exc).__name__)
            raise typography_errors.TypographyError("unreadable") from exc
        return record, Rendered(result.data, dict(result.headers()), plan.families())

    def substitutes(self, families: tuple[str, ...]) -> int:
        """How many of ``families`` the server renders with another font (or not at all)."""
        if not families:
            return 0
        try:
            catalog = self.fonts()
        except Exception as exc:  # noqa: BLE001 - a header only
            log.warning("font catalog unavailable (%s)", type(exc).__name__)
            return 0
        count = 0
        for family in families:
            status = catalog.find(family)
            if status is None or status.status != "installed":
                count += 1
        return count

    def close(self) -> None:
        self.jobs.close()


def _ai_disabled(found: DocumentSections) -> DocumentSections:
    """The detector said ``llm_unavailable``; the user switched the AI off: say that instead."""
    if "ai_disabled" in found.warnings:
        return found
    warnings = ["ai_disabled", *(w for w in found.warnings if w != "llm_unavailable")]
    return DocumentSections.build(
        doc_sha256=found.doc_sha256,
        model=found.model,
        blocks=found.blocks,
        labels=found.labels,
        warnings=warnings,
    )


def _close(llm: Any) -> None:
    close = getattr(llm, "close", None)
    if callable(close):
        try:
            close()
        except Exception as exc:  # noqa: BLE001 - cleanup only
            log.warning("LLM client not closed (%s)", type(exc).__name__)


__all__ = [
    "AnalyseOptions",
    "ExportService",
    "Rendered",
    "error_code",
    "error_message",
    "parse_analyse",
    "parse_plan_body",
]
