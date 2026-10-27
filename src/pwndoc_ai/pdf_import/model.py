"""Data model of the PDF importer: engines, precheck facts, metrics, the report and the
``auto`` engine choice.

Pure data (no pypdf, no PyMuPDF). ``from_json`` validates hostile input (the JSON report a
worker subprocess writes, or a stored file). The ``auto`` thresholds below were measured on six
real PDFs (docs/EXPORT_DESIGN.md (g)); thresholds marked *proposed* had no failing sample to
calibrate against and may be tuned, but only here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .. import export_json as jc
from ..export_errors import ExportInputError
from ..font_catalog_model import clean_family

ENGINES = ("auto", "structure", "pdf2docx")
CONCRETE_ENGINES = ("structure", "pdf2docx")
VERDICTS = ("text", "mixed", "scanned")
LICENCES = {"structure": "GPL/AGPL tools run as separate processes", "pdf2docx": "AGPL-3.0"}
ENGINE_NOTES = ("", "not_installed", "tools_missing")
REASONS = (
    "user_choice",
    "structure_ok",
    "structure_failed",
    "text_loss",
    "tabular",
    "sparse_pages",
    "columns",
    "fallback_unavailable",
    "pdf2docx_worse",
)
WARNINGS = (
    "mixed_scanned",  # some pages have no text layer: they were converted as images only
    "fallback_unavailable",  # auto wanted pdf2docx but it is not installed
    "no_headings",  # the engine found no heading (pdf2docx never finds any)
    "images_capped",  # more images than MAX_IMAGES / MAX_IMAGES_PER_PAGE: the rest skipped
    "tables_split",  # tables continuing over pages were kept as one table per page
    "running_lines_removed",  # repeated header/footer lines were dropped
    "tagged_pdf",  # the PDF had a structure tree (informational)
)

# auto selection (measured unless marked proposed)
SCANNED_SHARE_REFUSE = 0.8  # >= : "scanned", no engine runs
SCANNED_SHARE_MIXED = 0.1  # >  : "mixed", warn
TEXT_LOSS_FALLBACK = 0.03  # structure loss above this -> pdf2docx (measured 0.5-1.5%)
TABULAR_FALLBACK = 0.08  # residual tabular line share above this -> pdf2docx (proposed)
SPARSE_LINES_PER_PAGE = 8.0  # below, with images/page above 1.0 -> pdf2docx (proposed)
SPARSE_IMAGES_PER_PAGE = 1.0
MAX_COLUMNS_STRUCTURE = 2  # more columns -> pdf2docx (proposed)

MAX_PAGES_REPORT = 2000
MAX_FONTS_SEEN = 20
MAX_SECONDS_REPORT = 3600.0


def engine_choice(value: Any, path: str = "engine") -> str:
    """A requested engine (``auto``, ``structure`` or ``pdf2docx``); 400 ``engine`` otherwise."""
    if not isinstance(value, str) or value not in ENGINES:
        raise ExportInputError("engine", field=path)
    return value


def _ratio(value: Any, path: str) -> float:
    return jc.number(value, path, 0.0, 1.0)


def _codes(value: Any, path: str, allowed: tuple[str, ...]) -> tuple[str, ...]:
    codes = jc.items(value, path, len(allowed), lambda v, p: jc.choice(v, p, allowed))
    jc.unique(codes, path)
    return codes


@dataclass(frozen=True)
class EngineInfo:
    """One row of the ``engines`` list in ``GET /capabilities``."""

    name: str
    available: bool
    note: str = ""

    @property
    def licence(self) -> str:
        return LICENCES[self.name]

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "licence": self.licence,
            "note": self.note,
        }

    @classmethod
    def from_json(cls, value: Any, path: str = "engine") -> EngineInfo:
        data = jc.record(value, path, ("name", "available", "licence", "note"))
        name = jc.choice(data["name"], f"{path}.name", CONCRETE_ENGINES)
        if data["licence"] != LICENCES[name]:
            raise jc.fail(f"{path}.licence")
        return cls(
            name=name,
            available=jc.flag(data["available"], f"{path}.available"),
            note=jc.choice(data["note"], f"{path}.note", ENGINE_NOTES),
        )


@dataclass(frozen=True)
class PdfFacts:
    """What the precheck learned without rendering: page count (recounted, never ``/Count``),
    file size, whether a structure tree exists."""

    pages: int
    size: int
    tagged: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"pages": self.pages, "size": self.size, "tagged": self.tagged}

    @classmethod
    def from_json(cls, value: Any, path: str = "facts") -> PdfFacts:
        data = jc.record(value, path, ("pages", "size", "tagged"))
        pages = jc.count(data["pages"], f"{path}.pages", MAX_PAGES_REPORT)
        if pages < 1:
            raise jc.fail(f"{path}.pages")
        return cls(
            pages=pages,
            size=jc.count(data["size"], f"{path}.size", 1 << 31),
            tagged=jc.flag(data["tagged"], f"{path}.tagged"),
        )


METRIC_COUNTS = ("pages", "lines", "title", "tables", "images", "lists", "dropped_running_lines")


@dataclass(frozen=True)
class ConversionMetrics:
    """Measured on one engine's output. ``text_loss`` is the share of ``pdftotext`` tokens
    (NFKC, ligatures normalised, 2+ characters, dropped running lines added back) missing from
    the DOCX text. ``headings`` counts heading levels 1-4. ``residual_tabular`` is the share of
    lines with 3+ spans separated by more than 2.5 em that are not inside a detected table."""

    engine: str
    pages: int
    lines: int = 0
    headings: tuple[int, int, int, int] = (0, 0, 0, 0)
    title: int = 0
    tables: int = 0
    images: int = 0
    lists: int = 0
    text_loss: float = 0.0
    residual_tabular: float = 0.0
    max_columns: int = 1
    dropped_running_lines: int = 0
    seconds: float = 0.0

    @property
    def lines_per_page(self) -> float:
        return self.lines / self.pages if self.pages else 0.0

    @property
    def images_per_page(self) -> float:
        return self.images / self.pages if self.pages else 0.0

    @property
    def heading_total(self) -> int:
        return sum(self.headings) + self.title

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"engine": self.engine, "headings": list(self.headings)}
        data.update({key: getattr(self, key) for key in METRIC_COUNTS})
        data.update(
            text_loss=self.text_loss,
            residual_tabular=self.residual_tabular,
            max_columns=self.max_columns,
            seconds=self.seconds,
        )
        return data

    @classmethod
    def from_json(cls, value: Any, path: str = "metrics") -> ConversionMetrics:
        extra = ("text_loss", "residual_tabular", "max_columns", "seconds")
        data = jc.record(value, path, ("engine", "headings", *METRIC_COUNTS, *extra))
        heads = jc.items(data["headings"], f"{path}.headings", 4, jc.count)
        if len(heads) != 4:
            raise jc.fail(f"{path}.headings")
        counts = {key: jc.count(data[key], f"{path}.{key}") for key in METRIC_COUNTS}
        return cls(
            engine=jc.choice(data["engine"], f"{path}.engine", CONCRETE_ENGINES),
            headings=(heads[0], heads[1], heads[2], heads[3]),
            text_loss=_ratio(data["text_loss"], f"{path}.text_loss"),
            residual_tabular=_ratio(data["residual_tabular"], f"{path}.residual_tabular"),
            max_columns=jc.count(data["max_columns"], f"{path}.max_columns", 50),
            seconds=jc.number(data["seconds"], f"{path}.seconds", 0.0, MAX_SECONDS_REPORT),
            **counts,
        )


def _font(value: Any, path: str) -> str:
    name = clean_family(value, path)
    if not name:
        raise jc.fail(path)
    return name


def _fonts(value: Any, path: str) -> tuple[str, ...]:
    names = jc.items(value, path, MAX_FONTS_SEEN, _font)
    jc.unique(names, path)
    return names


@dataclass(frozen=True)
class ConversionReport:
    """The quality report shown in the Export tab and stored as ``report.json``. ``fonts_seen``
    are PDF font names reduced by ``font_catalog_model.strip_pdf_font_name``; the worker drops
    every name that does not pass ``clean_family`` before writing the report (hints only)."""

    engine_requested: str
    engine_used: str
    reason: str
    verdict: str
    scanned_share: float
    facts: PdfFacts
    metrics: ConversionMetrics
    fonts_seen: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.metrics.engine != self.engine_used or self.verdict == "scanned":
            raise ExportInputError("invalid", field="report")
        explicit = self.engine_requested != "auto"
        if explicit and (self.engine_used != self.engine_requested or self.reason != "user_choice"):
            raise ExportInputError("invalid", field="report.reason")
        if not explicit and self.reason == "user_choice":
            raise ExportInputError("invalid", field="report.reason")

    def to_json(self) -> dict[str, Any]:
        return {
            "engine_requested": self.engine_requested,
            "engine_used": self.engine_used,
            "reason": self.reason,
            "verdict": self.verdict,
            "scanned_share": self.scanned_share,
            "facts": self.facts.to_json(),
            "metrics": self.metrics.to_json(),
            "fonts_seen": list(self.fonts_seen),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_json(cls, value: Any, path: str = "report") -> ConversionReport:
        keys = ("engine_requested", "engine_used", "reason", "verdict", "scanned_share", "facts")
        data = jc.record(value, path, (*keys, "metrics", "fonts_seen", "warnings"))
        return cls(
            engine_requested=jc.choice(
                data["engine_requested"], f"{path}.engine_requested", ENGINES
            ),
            engine_used=jc.choice(data["engine_used"], f"{path}.engine_used", CONCRETE_ENGINES),
            reason=jc.choice(data["reason"], f"{path}.reason", REASONS),
            verdict=jc.choice(data["verdict"], f"{path}.verdict", VERDICTS),
            scanned_share=_ratio(data["scanned_share"], f"{path}.scanned_share"),
            facts=PdfFacts.from_json(data["facts"], f"{path}.facts"),
            metrics=ConversionMetrics.from_json(data["metrics"], f"{path}.metrics"),
            fonts_seen=_fonts(data["fonts_seen"], f"{path}.fonts_seen"),
            warnings=_codes(data["warnings"], f"{path}.warnings", WARNINGS),
        )


def verdict_for(scanned_share: float) -> str:
    """``scanned`` at >= 0.8 image-only pages, ``mixed`` above 0.1, else ``text``."""
    if scanned_share >= SCANNED_SHARE_REFUSE:
        return "scanned"
    return "mixed" if scanned_share > SCANNED_SHARE_MIXED else "text"


def structure_weakness(metrics: ConversionMetrics) -> str:
    """The first measurable reason the structure rebuild is poor, or ``""`` when it is fine.
    Zero headings is NOT a reason (pdf2docx finds none either)."""
    if metrics.text_loss > TEXT_LOSS_FALLBACK:
        return "text_loss"
    if metrics.residual_tabular > TABULAR_FALLBACK:
        return "tabular"
    sparse = metrics.lines_per_page < SPARSE_LINES_PER_PAGE
    if sparse and metrics.images_per_page > SPARSE_IMAGES_PER_PAGE:
        return "sparse_pages"
    if metrics.max_columns > MAX_COLUMNS_STRUCTURE:
        return "columns"
    return ""


def keep_pdf2docx(structure: ConversionMetrics | None, pdf2docx: ConversionMetrics) -> bool:
    """After an ``auto`` fallback both results exist: keep pdf2docx unless it lost more text
    than the structure rebuild (then the structure result wins, reason ``pdf2docx_worse``)."""
    return structure is None or pdf2docx.text_loss <= structure.text_loss


__all__ = [
    "CONCRETE_ENGINES",
    "ENGINES",
    "MAX_COLUMNS_STRUCTURE",
    "REASONS",
    "SCANNED_SHARE_MIXED",
    "SCANNED_SHARE_REFUSE",
    "SPARSE_IMAGES_PER_PAGE",
    "SPARSE_LINES_PER_PAGE",
    "TABULAR_FALLBACK",
    "TEXT_LOSS_FALLBACK",
    "VERDICTS",
    "WARNINGS",
    "ConversionMetrics",
    "ConversionReport",
    "EngineInfo",
    "PdfFacts",
    "engine_choice",
    "keep_pdf2docx",
    "structure_weakness",
    "verdict_for",
]
