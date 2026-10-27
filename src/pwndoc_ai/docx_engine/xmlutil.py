"""Namespaces, safe XML parsing and small element helpers shared by the engine modules."""

from __future__ import annotations

from lxml import etree

from . import limits
from .errors import DocxError

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
SPACE = f"{{{XML_NS}}}space"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"

_PARSER_ARGS = {
    "resolve_entities": False,
    "no_network": True,
    "load_dtd": False,
    "dtd_validation": False,
    "huge_tree": False,
}


def w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def local(element: etree._Element) -> str:
    tag = element.tag
    return tag.rpartition("}")[2] if isinstance(tag, str) else ""


def attr(element: etree._Element, name: str) -> str | None:
    return element.get(w(name))


def parse_xml(data: bytes) -> etree._Element:
    """Parse untrusted XML: no entities, no DTD, no network, no huge nodes. Raises DocxError."""
    try:
        root = etree.fromstring(data, etree.XMLParser(**_PARSER_ARGS))
    except (etree.XMLSyntaxError, ValueError):
        raise DocxError("The file is damaged or is not a valid Word document.") from None
    tree = root.getroottree()
    if tree.docinfo.doctype or tree.docinfo.internalDTD is not None:
        raise DocxError("The file contains unsupported XML declarations.")
    if any(True for _ in root.iter(etree.Entity)):
        raise DocxError("The file contains unsupported XML declarations.")
    count = 0
    for _ in root.iter():
        count += 1
        if count > limits.MAX_ELEMENTS:
            raise DocxError("The document structure is too large to process.")
    return root
