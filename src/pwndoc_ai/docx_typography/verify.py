"""Output checks: every edited part still parses, its paragraph text sequence is unchanged, and
python-docx reopens the package. Any failure is ``TypographyError("verify_failed")``."""

from __future__ import annotations

import io
from collections.abc import Iterable

from lxml import etree

from ..docx_engine import limits as engine_limits
from ..docx_engine.errors import DocxError
from ..docx_engine.package import Package, open_package
from ..docx_engine.xmlutil import parse_xml, w
from .errors import TypographyError

TEXT_TAGS = (w("t"), w("delText"), w("instrText"), w("tab"), w("br"), w("sym"))


def _paragraph_text(paragraph: etree._Element) -> str:
    parts = []
    for node in paragraph.iter(*TEXT_TAGS):
        parts.append(node.text or "" if node.tag != w("tab") else "\t")
    return "".join(parts)


def text_sequence(root: etree._Element) -> tuple[str, ...]:
    """The text of every paragraph (nested ones included) in document order."""
    return tuple(_paragraph_text(p) for p in root.iter(w("p")))


def _part_text(package: Package, name: str) -> tuple[str, ...]:
    return text_sequence(parse_xml(package.read(name, engine_limits.MAX_XML_BYTES)))


def _reopen(data: bytes) -> None:
    try:
        import docx  # noqa: PLC0415  (optional dependency: the "docx" extra)
    except ImportError:
        return
    try:
        docx.Document(io.BytesIO(data))
    except Exception:  # noqa: BLE001  (python-docx raises many types on a broken package)
        raise TypographyError("verify_failed") from None


def verify_output(original: Package, output: bytes, edited: Iterable[str]) -> None:
    """Raise ``TypographyError("verify_failed")`` unless ``output`` is safe to serve."""
    try:
        package = open_package(output)
        for name in edited:
            if _part_text(original, name) != _part_text(package, name):
                raise TypographyError("verify_failed")
    except DocxError:
        raise TypographyError("verify_failed") from None
    _reopen(output)
