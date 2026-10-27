"""The body block enumeration shared with section detection (docs/EXPORT_DESIGN.md (h)).

One block per top-level ``w:body`` child that is a ``w:p`` or a ``w:tbl``. Block-level content
controls (``w:sdt``) are unwrapped (their ``w:sdtContent`` children are enumerated in place);
``w:sectPr`` and every other element are skipped. Ids are ``b%05d`` in document order.
"""

from __future__ import annotations

from collections.abc import Iterator

from lxml import etree

from ..docx_engine.xmlutil import local, w

BLOCK_KINDS = ("p", "tbl")
MAX_SDT_DEPTH = 8


def block_id(index: int) -> str:
    return f"b{index:05d}"


def _items(parent: etree._Element, depth: int) -> Iterator[etree._Element]:
    for child in parent:
        name = local(child)
        if name in BLOCK_KINDS:
            yield child
        elif name == "sdt" and depth < MAX_SDT_DEPTH:
            content = child.find(w("sdtContent"))
            if content is not None:
                yield from _items(content, depth + 1)


def body_blocks(root: etree._Element) -> list[tuple[str, etree._Element]]:
    """``[(block_id, element)]`` for a ``w:document`` (or its ``w:body``) element."""
    body = root.find(w("body")) if local(root) == "document" else root
    if body is None or local(body) != "body":
        return []
    return [(block_id(index), item) for index, item in enumerate(_items(body, 0))]


__all__ = ["block_id", "body_blocks"]
