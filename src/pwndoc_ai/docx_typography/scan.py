"""What the document uses today: families and sizes of its text runs (the UI "current" column)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator

from lxml import etree

from ..docx_engine.xmlutil import w
from ..export_errors import ExportInputError
from ..font_catalog_model import clean_family
from . import budget
from . import rpr as rp
from .limits import MAX_FONT_USES, WORD_DEFAULT_HALF_POINTS
from .load import open_doc
from .model import FontUse
from .runs_edit import SKIPPED
from .stylebook import StyleBook
from .theme_edit import theme_fonts


def _text_runs(root: etree._Element) -> Iterator[tuple[etree._Element, etree._Element]]:
    for paragraph in root.iter(w("p")):
        budget.check()
        for run in paragraph.iter(w("r")):
            has_text = any((t.text or "").strip() for t in run.iter(w("t")))
            parent = run.getparent()
            if has_text and (parent is None or etree.QName(parent).localname not in SKIPPED):
                yield paragraph, run


def _run_values(paragraph, run, book: StyleBook, theme) -> tuple[str | None, int]:  # noqa: ANN001
    props = run.find(w("rPr"))
    style = props.find(w("rStyle")) if props is not None else None
    char = book.chain_values(style.get(w("val")) if style is not None else None, theme)
    para = book.effective(book.paragraph_style(paragraph), theme)
    family = rp.latin(props, theme) or char.family or para.family
    size = rp.size(props) or char.size or para.size or WORD_DEFAULT_HALF_POINTS
    return family, size


def _scan(data: bytes) -> tuple[Counter, Counter]:
    doc = open_doc(data)
    book, theme = StyleBook(doc.styles), theme_fonts(doc.theme)
    families: Counter = Counter()
    sizes: Counter = Counter()
    for paragraph, run in _text_runs(doc.root):
        family, size = _run_values(paragraph, run, book, theme)
        sizes[size / 2] += 1
        try:
            name = clean_family(family or "", "family")
        except ExportInputError:
            continue
        if name:
            families[name] += 1
    return families, sizes


def document_typography(
    data: bytes, *, seconds: float | None = None
) -> tuple[tuple[FontUse, ...], tuple[tuple[float, int], ...]]:
    """``(fonts, sizes)``: the families and point sizes of the body text runs, most used first
    (at most ``MAX_FONT_USES`` each). Raises ``TypographyError``."""
    with budget.limit(seconds):
        families, sizes = _scan(data)
    fonts = tuple(FontUse(n, c) for n, c in families.most_common(MAX_FONT_USES))
    return fonts, tuple(sizes.most_common(MAX_FONT_USES))


def fonts_used(data: bytes) -> tuple[FontUse, ...]:
    """The families the body text uses (effective: run, character style, paragraph style,
    docDefaults, theme), with the number of text runs in each."""
    return document_typography(data)[0]


def sizes_used(data: bytes) -> tuple[tuple[float, int], ...]:
    """``((points, runs), ...)`` for the body text, most used first."""
    return document_typography(data)[1]
