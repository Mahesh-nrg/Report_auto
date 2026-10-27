"""Apply a ``TypographyPlan`` to a DOCX (docs/EXPORT_DESIGN.md (i)).

Only ``word/document.xml`` (the main part), ``styles.xml``, the theme part and the header, footer,
footnote and endnote parts are ever rewritten, and only when something in them changed; every other
member keeps its bytes (``write_package``). Numbering parts are never edited, so bullet glyph fonts
stay as they are. An empty plan, or a plan that changes nothing, returns the input bytes.
"""

from __future__ import annotations

from collections.abc import Mapping

from lxml import etree

from ..docx_engine.errors import DocxError
from ..docx_engine.package import write_package
from . import budget
from .blocks import body_blocks
from .errors import TypographyError
from .infer import infer_type
from .load import TypoDoc, open_doc, read_part
from .model import BlockAssignment, TypographyPlan, TypographyResult, check_assignments
from .runs_edit import RunEditor
from .stylebook import StyleBook
from .styles_edit import edit_styles, snapshot
from .theme_edit import edit_theme, theme_fonts
from .verify import verify_output

PART_TYPES = (
    ("header", "header"),
    ("footer", "footer"),
    ("footnotes", "footnote"),
    ("endnotes", "footnote"),
)


def _serialize(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def apply_plan(
    data: bytes,
    plan: TypographyPlan,
    assignments: Mapping[str, BlockAssignment] | None = None,
    *,
    seconds: float | None = None,
) -> TypographyResult:
    """The retypeset DOCX. Raises ``TypographyError`` (unreadable, too_large, too_complex,
    verify_failed). ``assignments=None`` infers block types from styles (no sections)."""
    if not isinstance(plan, TypographyPlan):
        raise TypeError("plan must be a TypographyPlan")
    chosen = check_assignments(assignments) if assignments is not None else None
    if plan.is_empty:
        return TypographyResult(data=data)
    with budget.limit(seconds):
        return _apply(data, plan, chosen)


def _block_types(doc: TypoDoc, book: StyleBook, chosen, theme) -> list:  # noqa: ANN001
    """``[(element, block_type, section_id)]``, read before any style is edited."""
    found = []
    for ident, element in body_blocks(doc.root):
        budget.check()
        assigned = chosen.get(ident) if chosen is not None else None
        if assigned is not None:
            found.append((element, assigned.block_type, assigned.section_id))
        else:
            found.append((element, infer_type(element, book, theme), None))
    return found


def _edit_parts(doc: TypoDoc, editor: RunEditor) -> dict[str, bytes]:
    changed: dict[str, bytes] = {}
    for kind, block_type in PART_TYPES:
        for name in doc.part_names(kind):
            root = read_part(doc, name)
            before = editor.edits
            if root is not None:
                editor.edit_part(root, block_type)
            if root is not None and editor.edits != before:
                changed[name] = _serialize(root)
    return changed


def _apply(data: bytes, plan: TypographyPlan, chosen) -> TypographyResult:  # noqa: ANN001
    doc = open_doc(data)
    book = StyleBook(doc.styles)
    theme_before = theme_fonts(doc.theme)
    style_route = doc.styles is not None and not plan.by_section
    blocks = _block_types(doc, book, chosen, theme_before)
    replacements: dict[str, bytes] = {}
    styles_changed, theme_changed = 0, False
    if style_route:
        before = snapshot(book, theme_before)
        theme_changed = edit_theme(doc.theme, plan)
        styles_changed = edit_styles(book, plan, before, theme_fonts(doc.theme))
    theme = theme_fonts(doc.theme)
    editor = RunEditor(plan, book, theme, style_route)
    for element, block_type, section in blocks:
        budget.check()
        editor.edit_block(element, block_type, section)
    main_edits = editor.edits
    replacements.update(_edit_parts(doc, editor))
    if main_edits:
        replacements[doc.loaded.package.main_part] = _serialize(doc.root)
    if styles_changed and doc.styles_name:
        replacements[doc.styles_name] = _serialize(doc.styles)
    if theme_changed and doc.theme_name:
        replacements[doc.theme_name] = _serialize(doc.theme)
    if not replacements:
        return TypographyResult(data=data, symbol_runs_kept=editor.symbols)
    return _finish(doc, replacements, editor, styles_changed, theme_changed, plan)


def _finish(doc, replacements, editor, styles_changed, theme_changed, plan):  # noqa: ANN001, ANN202
    budget.check()
    try:
        output = write_package(doc.loaded.package, replacements)
    except DocxError:
        raise TypographyError("unreadable") from None
    text_parts = [name for name in replacements if name not in (doc.styles_name, doc.theme_name)]
    verify_output(doc.loaded.package, output, text_parts)
    return TypographyResult(
        data=output,
        runs_changed=editor.runs,
        styles_changed=styles_changed,
        theme_changed=theme_changed,
        symbol_runs_kept=editor.symbols,
        families=plan.families(),
    )
