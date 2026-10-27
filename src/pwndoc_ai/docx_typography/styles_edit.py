"""Style-level edits for a plan without sections (docs/EXPORT_DESIGN.md (i), "Where edits go" 1).

``docDefaults`` takes the body spec; then every paragraph and table style is brought to its
*desired* value: the plan's spec for the block type the style stands for where the plan sets one,
otherwise the value the style had before any edit. Styles are visited parents first, and a style is
only written when its effective value (chain plus docDefaults) differs from the desired one, so a
heading keeps its own size when the default size changes, code keeps its font when the default
family changes, and a second run changes nothing.
"""

from __future__ import annotations

from ..docx_engine.xmlutil import w
from . import budget
from . import rpr as rp
from .model import FontSpec, TypographyPlan
from .stylebook import Effective, StyleBook

EDITED_KINDS = ("paragraph", "table")
Snapshot = dict[str, Effective]


def _targets(book: StyleBook) -> list[str]:
    idents = [i for i, s in book.styles.items() if s.get(w("type")) in EDITED_KINDS]
    return sorted(idents, key=lambda ident: len(book.chain(ident)))


def snapshot(book: StyleBook, theme: rp.ThemeFonts) -> Snapshot:
    """The effective values of every edited style before anything changes."""
    return {ident: book.effective(ident, theme) for ident in _targets(book)}


def _style_type(book: StyleBook, ident: str) -> str:
    if book.styles[ident].get(w("type")) == "table":
        return "table"
    return book.style_type(ident)


def _same(left: str | None, right: str | None) -> bool:
    return (left or "").casefold() == (right or "").casefold()


def _edit_defaults(book: StyleBook, spec: FontSpec) -> bool:
    if spec.is_empty:
        return False
    props = book.defaults_rpr(create=True)
    changed = bool(spec.family) and rp.set_family(props, spec.family)
    if spec.half_points is not None:
        changed = rp.set_size(props, spec.half_points) or changed
    return changed


def _edit_style(book: StyleBook, ident: str, spec: FontSpec, old: Effective, theme) -> bool:  # noqa: ANN001
    style = book.styles[ident]
    family = spec.family or old.family
    size = spec.half_points if spec.half_points is not None else old.size
    now = book.effective(ident, theme)
    changed = False
    own = rp.explicit_family(style.find(w("rPr")))
    if family and (not _same(now.family, family) or (own and spec.family)):
        changed = rp.set_family(rp.style_rpr(style), family)
    if size is not None and now.size != size:
        changed = rp.set_size(rp.style_rpr(style), size) or changed
    if size is not None and spec.half_points is not None:
        changed = rp.fit_line(style.find(w("pPr")), size) or changed
    return changed


def edit_styles(
    book: StyleBook, plan: TypographyPlan, before: Snapshot, theme: rp.ThemeFonts
) -> int:
    """Apply ``plan`` at style level. Returns the number of styles (docDefaults counts as one)
    that changed. ``theme`` is the theme map after any theme edit."""
    if book.root is None:
        return 0
    changed = int(_edit_defaults(book, plan.resolve("body")))
    for ident in _targets(book):
        budget.check()
        spec = plan.resolve(_style_type(book, ident))
        if ident in before and _edit_style(book, ident, spec, before[ident], theme):
            changed += 1
    return changed
