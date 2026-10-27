"""Run-level edits (docs/EXPORT_DESIGN.md (i), "Where edits go" 2 and 3).

In a paragraph whose style already carries the resolved spec ("covered"), only runs that override
the style (an explicit ``w:rFonts``/``w:sz``, or a character style that sets them) are edited.
Elsewhere (section plans, table blocks, a type that differs from the style's type, documents
without styles) every run gets the spec explicitly. Runs inside tracked changes, drawings, text
boxes and alternate content, ``w:sym`` runs, runs with an ``rPrChange`` and runs in a symbol font
are never touched; text, fields, bookmarks and table structure are never touched at all.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from lxml import etree

from ..docx_engine.xmlutil import local, w
from . import budget
from . import rpr as rp
from .limits import is_symbol_font
from .model import FontSpec, TypographyPlan
from .stylebook import Effective, StyleBook

SKIPPED = frozenset(
    {"ins", "del", "moveFrom", "moveTo", "drawing", "pict", "txbxContent", "AlternateContent",
     "object", "rPrChange"}
)  # fmt: skip
NO_STYLE = Effective(None, None)


def _skipped(node: etree._Element, stop: etree._Element) -> bool:
    current = node.getparent()
    while current is not None and current is not stop:
        if local(current) in SKIPPED:
            return True
        current = current.getparent()
    return False


def _same(left: str | None, right: str | None) -> bool:
    return (left or "").casefold() == (right or "").casefold()


class RunEditor:
    """Applies resolved specs to paragraphs. Counters are read by ``apply`` afterwards."""

    def __init__(
        self, plan: TypographyPlan, book: StyleBook, theme: rp.ThemeFonts, style_route: bool
    ) -> None:
        self.plan, self.book, self.theme, self.style_route = plan, book, theme, style_route
        self.runs = 0
        self.symbols = 0
        self.edits = 0

    def paragraphs(self, element: etree._Element) -> Iterator[etree._Element]:
        if local(element) == "p":
            yield element
            return
        for paragraph in element.iter(w("p")):
            if not _skipped(paragraph, element):
                yield paragraph

    def edit_block(self, element: etree._Element, block_type: str, section: str | None) -> None:
        spec = self.plan.resolve(block_type, section)
        if spec.is_empty:
            return
        force = local(element) == "tbl"
        for paragraph in self.paragraphs(element):
            self.edit_paragraph(paragraph, spec, force)

    def edit_part(self, root: etree._Element, block_type: str) -> None:
        spec = self.plan.resolve(block_type)
        if spec.is_empty:
            return
        for paragraph in self.paragraphs(root):
            budget.check()
            self.edit_paragraph(paragraph, spec, False)

    def _covered(self, paragraph: etree._Element, spec: FontSpec, force: bool) -> tuple[bool, bool]:
        if force or not self.style_route:
            return False, False
        ident = self.book.paragraph_style(paragraph)
        style_spec = self.plan.resolve(self.book.style_type(ident))
        return _same(style_spec.family, spec.family), style_spec.size == spec.size

    def edit_paragraph(self, paragraph: etree._Element, spec: FontSpec, force: bool) -> None:
        covered = self._covered(paragraph, spec, force)
        ppr = paragraph.find(w("pPr"))
        mark = ppr.find(w("rPr")) if ppr is not None else None
        if mark is None or mark.find(w("rPrChange")) is None:
            self._apply(mark, lambda: rp.mark_rpr(paragraph), spec, covered, NO_STYLE)
        for run in paragraph.iter(w("r")):
            if not _skipped(run, paragraph):
                self.edit_run(run, spec, covered)
        if spec.half_points is not None and rp.fit_line(paragraph.find(w("pPr")), spec.half_points):
            self.edits += 1

    def _char_style(self, props: etree._Element | None) -> Effective:
        node = props.find(w("rStyle")) if props is not None else None
        if node is None:
            return NO_STYLE
        return self.book.chain_values(node.get(w("val")), self.theme)

    def edit_run(self, run: etree._Element, spec: FontSpec, covered: tuple[bool, bool]) -> None:
        props = run.find(w("rPr"))
        if run.find(w("sym")) is not None:
            self.symbols += 1
            return
        if props is not None and props.find(w("rPrChange")) is not None:
            return
        char = self._char_style(props)
        if is_symbol_font(rp.latin(props, self.theme) or char.family):
            self.symbols += 1
            return
        if self._apply(props, lambda: rp.run_rpr(run), spec, covered, char):
            self.runs += 1

    def _apply(
        self,
        props: etree._Element | None,
        make: Callable[[], etree._Element],
        spec: FontSpec,
        covered: tuple[bool, bool],
        char: Effective,
    ) -> bool:
        family = bool(spec.family) and (
            not covered[0] or rp.explicit_family(props) or bool(char.family)
        )
        size = spec.half_points is not None and (
            not covered[1] or rp.explicit_size(props) or char.size is not None
        )
        changed = False
        if family:
            changed = rp.set_family(make(), spec.family)
        if size:
            changed = rp.set_size(make(), spec.half_points) or changed
        self.edits += int(changed)
        return changed
