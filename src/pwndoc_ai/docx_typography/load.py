"""Open a DOCX for the typography engine: package, main part, styles and theme roots."""

from __future__ import annotations

from dataclasses import dataclass

from lxml import etree

from ..docx_engine.errors import DocxError
from ..docx_layout.errors import DocxLayoutError
from ..docx_layout.load import LoadedDoc, load_document, read_xml
from .errors import TypographyError
from .limits import MAX_PACKAGE_BYTES, MAX_PARTS_PER_KIND


@dataclass(frozen=True)
class TypoDoc:
    loaded: LoadedDoc
    styles_name: str | None
    styles: etree._Element | None
    theme_name: str | None
    theme: etree._Element | None

    @property
    def root(self) -> etree._Element:
        return self.loaded.root

    def part_names(self, kind: str) -> tuple[str, ...]:
        return self.loaded.parts.get(kind, ())[:MAX_PARTS_PER_KIND]


def _first(loaded: LoadedDoc, kind: str) -> tuple[str | None, etree._Element | None]:
    names = loaded.parts.get(kind, ())
    if not names:
        return None, None
    return names[0], read_xml(loaded.package, names[0])


def open_doc(data: bytes) -> TypoDoc:
    """Raises ``TypographyError`` (``too_large`` or ``unreadable``)."""
    if not isinstance(data, bytes | bytearray) or not data:
        raise TypographyError("unreadable")
    if len(data) > MAX_PACKAGE_BYTES:
        raise TypographyError("too_large")
    try:
        loaded = load_document(bytes(data))
        styles_name, styles = _first(loaded, "styles")
        theme_name, theme = _first(loaded, "theme")
    except (DocxLayoutError, DocxError):
        raise TypographyError("unreadable") from None
    return TypoDoc(loaded, styles_name, styles, theme_name, theme)


def read_part(doc: TypoDoc, name: str) -> etree._Element | None:
    try:
        return read_xml(doc.loaded.package, name)
    except (DocxLayoutError, DocxError):
        raise TypographyError("unreadable") from None
