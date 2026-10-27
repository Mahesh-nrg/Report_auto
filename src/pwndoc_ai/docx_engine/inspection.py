"""inspect_docx: validate an untrusted DOCX and describe its paragraphs, outline and statistics."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from lxml import etree

from . import limits
from .diff_edit import can_add_after
from .errors import DocxError
from .model import Inspection, Section, Unit
from .package import Package, open_package
from .protect import protection_reason
from .runs import paragraph_text
from .styles import StyleMap
from .xmlutil import MC_NS, attr, parse_xml, w

HEADING_NAME = re.compile(r"^heading\s*([1-9])$", re.IGNORECASE)
START_TITLE = "Start of document"


@dataclass(frozen=True)
class LoadedDocument:
    """A validated document: the package, its parsed body and what was found in it."""

    package: Package
    root: etree._Element
    paragraphs: tuple[etree._Element, ...]
    inspection: Inspection


def _styles(package: Package) -> StyleMap:
    name = posixpath.join(posixpath.dirname(package.main_part), "styles.xml")
    if name not in package.names:
        return StyleMap.empty()
    return StyleMap.from_root(parse_xml(package.read(name, limits.MAX_SMALL_XML_BYTES)))


def _style_id(paragraph: etree._Element) -> str | None:
    style = paragraph.find(f"{w('pPr')}/{w('pStyle')}")
    return attr(style, "val") if style is not None else None


def _heading_level(
    paragraph: etree._Element, style_id: str | None, styles: StyleMap, text: str
) -> int | None:
    if not text.strip():
        return None
    outline = paragraph.find(f"{w('pPr')}/{w('outlineLvl')}")
    direct = attr(outline, "val") if outline is not None else None
    if direct is not None and direct.isdigit():
        return int(direct) + 1 if int(direct) <= 8 else None
    match = HEADING_NAME.match(styles.name(style_id))
    if match:
        return int(match.group(1))
    level = styles.outline_level(style_id)
    return level + 1 if level is not None and level <= 8 else None


def _unit(index: int, paragraph: etree._Element, styles: StyleMap) -> Unit:
    text = paragraph_text(paragraph)
    style_id = _style_id(paragraph)
    heading = _heading_level(paragraph, style_id, styles, text)
    return Unit(
        id=f"p{index:04d}",
        text=text,
        style=styles.name(style_id),
        heading_level=heading,
        protected=protection_reason(paragraph, style_id, styles, text),
        in_table=any(True for _ in paragraph.iterancestors(w("tc"))),
        addable=can_add_after(paragraph, heading=heading is not None),
    )


def _title(text: str) -> str:
    return " ".join(text.split())[: limits.MAX_SECTION_TITLE]


def _make_section(index: int, units: list[Unit], first: int, last: int) -> Section:
    head = units[first]
    refinable = [u for u in units[first : last + 1] if u.protected is None]
    starts_with_heading = head.heading_level is not None
    return Section(
        id=f"s{index}",
        title=_title(head.text) if starts_with_heading else START_TITLE,
        level=head.heading_level or 0,
        units=len(refinable),
        chars=sum(len(u.text) for u in refinable),
        first_unit=first,
        last_unit=last,
    )


def build_sections(units: list[Unit]) -> tuple[Section, ...]:
    """Flat sections: each heading starts one; paragraphs before the first heading form ``s0``."""
    starts = [i for i, u in enumerate(units) if u.heading_level is not None]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    sections = []
    for number, first in enumerate(starts):
        last = starts[number + 1] - 1 if number + 1 < len(starts) else len(units) - 1
        sections.append(_make_section(number, units, first, last))
    return tuple(sections)


def _count_images(root: etree._Element) -> int:
    count = 0
    for element in root.iter(w("drawing"), w("pict")):
        if not any(a.tag == f"{{{MC_NS}}}Fallback" for a in element.iterancestors()):
            count += 1
    return count


def _stats(
    root: etree._Element, units: list[Unit], sections: tuple[Section, ...]
) -> dict[str, int]:
    protected = sum(1 for u in units if u.protected)
    return {
        "paragraphs": len(units),
        "refinable": len(units) - protected,
        "protected": protected,
        "tables": sum(1 for _ in root.iter(w("tbl"))),
        "images": _count_images(root),
        "sections": len(sections),
        "words": sum(len(u.text.split()) for u in units),
    }


def load_document(data: bytes) -> LoadedDocument:
    """Validate ``data`` and parse its body. Raises DocxError (user-safe message)."""
    package = open_package(data)
    root = parse_xml(package.read(package.main_part, limits.MAX_XML_BYTES))
    paragraphs = tuple(root.iter(w("p")))
    if len(paragraphs) > limits.MAX_PARAGRAPHS:
        raise DocxError("The document has too many paragraphs.")
    if not any(paragraph_text(p).strip() for p in paragraphs):
        raise DocxError("The document has no text to refine.")
    styles = _styles(package)
    units = [_unit(i, p, styles) for i, p in enumerate(paragraphs)]
    sections = build_sections(units)
    inspection = Inspection(tuple(units), sections, _stats(root, units, sections))
    return LoadedDocument(package, root, paragraphs, inspection)


def inspect_docx(data: bytes) -> Inspection:
    """Describe an untrusted DOCX (paragraph units, outline sections, statistics)."""
    return load_document(data).inspection
