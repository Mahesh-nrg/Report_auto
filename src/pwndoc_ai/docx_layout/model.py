"""Public data types of the layout engine. All of them are frozen."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LayoutIssue:
    """One finding. ``message`` is short and user-safe; ``where`` is a paragraph position or a
    heading text of at most 40 characters."""

    code: str
    severity: str  # "error" | "warning" | "info"
    message: str
    fixable: bool
    count: int = 1
    where: str = ""


@dataclass(frozen=True)
class FontIssue:
    """A requested font family and what the renderer substitutes for it."""

    font: str
    resolved: str
    compatible: bool
    advice: str


@dataclass(frozen=True)
class LayoutReport:
    issues: tuple[LayoutIssue, ...]
    fonts: tuple[FontIssue, ...]
    stats: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class TidyOptions:
    collapse_empty_runs: bool = True
    fix_page_breaks: bool = True
    drop_empty_sections: bool = True
    keep_headings_with_next: bool = True
    keep_table_rows_together: bool = False
    update_toc_flag: bool = False


@dataclass(frozen=True)
class TidyResult:
    data: bytes
    fixes: tuple[LayoutIssue, ...]
    report_before: LayoutReport
    report_after: LayoutReport
