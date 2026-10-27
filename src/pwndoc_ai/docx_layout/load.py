"""Open a DOCX safely (same package rules and caps as the refine engine) and expose its parts."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field

from lxml import etree

from pwndoc_ai.docx_engine import limits
from pwndoc_ai.docx_engine.errors import DocxError
from pwndoc_ai.docx_engine.package import Package, open_package
from pwndoc_ai.docx_engine.xmlutil import REL_NS

from .errors import DocxLayoutError
from .ooxml import local, parse_xml, w
from .styles import StyleIndex

MAX_EXTRA_PARTS = 20
REL_KINDS = (
    "styles",
    "numbering",
    "theme",
    "header",
    "footer",
    "footnotes",
    "endnotes",
    "comments",
    "settings",
)


@dataclass(frozen=True)
class LoadedDoc:
    """A parsed document. ``root`` is a private working copy: only the tidy modules edit it."""

    package: Package
    root: etree._Element
    body: etree._Element
    styles: StyleIndex
    parts: dict[str, tuple[str, ...]]
    cache: dict[str, object] = field(default_factory=dict, compare=False, repr=False)

    def part_roots(self, kind: str) -> list[etree._Element]:
        roots: list[etree._Element] = []
        for name in self.parts.get(kind, ())[:MAX_EXTRA_PARTS]:
            root = read_xml(self.package, name)
            if root is not None:
                roots.append(root)
        return roots


def _safe(call):  # noqa: ANN001, ANN202
    try:
        return call()
    except DocxError as exc:
        raise DocxLayoutError(str(exc)) from None


def read_xml(package: Package, name: str) -> etree._Element | None:
    """A small XML part parsed safely, or None when the package has no such part."""
    if name not in package.names:
        return None
    return _safe(lambda: parse_xml(package.read(name, limits.MAX_SMALL_XML_BYTES)))


def _related_parts(package: Package) -> dict[str, tuple[str, ...]]:
    base = posixpath.dirname(package.main_part)
    rels_name = posixpath.join(base, "_rels", posixpath.basename(package.main_part) + ".rels")
    rels = read_xml(package, rels_name)
    found: dict[str, list[str]] = {kind: [] for kind in REL_KINDS}
    if rels is None:
        return {kind: () for kind in REL_KINDS}
    for rel in rels.iter(f"{{{REL_NS}}}Relationship"):
        kind = (rel.get("Type") or "").rpartition("/")[2]
        target = rel.get("Target") or ""
        if kind not in found or rel.get("TargetMode") == "External" or not target:
            continue
        path = target.lstrip("/") if target.startswith("/") else posixpath.join(base, target)
        path = posixpath.normpath(path)
        if path in package.names and path not in found[kind]:
            found[kind].append(path)
    return {kind: tuple(names) for kind, names in found.items()}


def _main_root(package: Package) -> etree._Element:
    root = _safe(lambda: parse_xml(package.read(package.main_part, limits.MAX_XML_BYTES)))
    if local(root) != "document" or root.find(w("body")) is None:
        raise DocxLayoutError("The file is not a Word document.")
    paragraphs = sum(1 for _ in root.iter(w("p")))
    if paragraphs > limits.MAX_PARAGRAPHS:
        raise DocxLayoutError("The document is too large or complex.")
    return root


def load_document(data: bytes) -> LoadedDoc:
    """Validate the package and parse the main part. Raises DocxLayoutError (user-safe)."""
    if not isinstance(data, bytes | bytearray):
        raise DocxLayoutError("The file is not a valid Word document.")
    package = _safe(lambda: open_package(bytes(data)))
    root = _main_root(package)
    parts = _related_parts(package)
    styles_root = read_xml(package, parts["styles"][0]) if parts["styles"] else None
    return LoadedDoc(package, root, root.find(w("body")), StyleIndex(styles_root), parts)
