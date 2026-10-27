"""The closed set of block types shared by section detection, the typography plan and the UI.

A block is one top-level body item of a DOCX (a paragraph or a whole table, see
docs/EXPORT_DESIGN.md (h)). Section detection labels every block with exactly one of these;
the typography plan maps them to fonts. Header, footer and footnote parts of the package are
always typed ``header``/``footer``/``footnote`` by the typography engine; the AI may also label a
*body* block ``header``/``footer`` (a running line left in the body by a PDF conversion).
"""

from __future__ import annotations

BLOCK_TYPES: tuple[str, ...] = (
    "title",
    "heading_1",
    "heading_2",
    "heading_3",
    "heading_4",
    "body",
    "list",
    "table",
    "code",
    "caption",
    "quote",
    "toc",
    "header",
    "footer",
    "footnote",
    "figure",
    "other",
)
HEADING_TYPES: tuple[str, ...] = ("title", "heading_1", "heading_2", "heading_3", "heading_4")
MAX_HEADING_LEVEL = 4

# Which block types inherit the plan-wide (or section-wide) default. The default family applies
# to every type except ``code`` (code keeps a monospace font unless ``code`` is set itself). The
# default size applies only to running text: headings, captions, the TOC, headers, footers and
# footnotes keep their own size unless their type is set explicitly.
DEFAULT_FAMILY_TYPES = frozenset(BLOCK_TYPES) - {"code"}
DEFAULT_SIZE_TYPES = frozenset(("body", "list", "table", "quote", "other"))

BLOCK_TYPE_LABELS: dict[str, str] = {
    "title": "Title",
    "heading_1": "Heading 1",
    "heading_2": "Heading 2",
    "heading_3": "Heading 3",
    "heading_4": "Heading 4 and below",
    "body": "Body text",
    "list": "Lists",
    "table": "Tables",
    "code": "Code",
    "caption": "Captions",
    "quote": "Quotes",
    "toc": "Table of contents",
    "header": "Page header",
    "footer": "Page footer",
    "footnote": "Footnotes",
    "figure": "Figures",
    "other": "Other",
}


def heading_type(level: int) -> str:
    """``heading_1`` .. ``heading_4`` for an outline level; 5 and deeper map to ``heading_4``."""
    return f"heading_{max(1, min(MAX_HEADING_LEVEL, int(level)))}"


def heading_level(block_type: str) -> int:
    """1-4 for a heading type, 0 for ``title`` and every other type."""
    if block_type.startswith("heading_"):
        return int(block_type.rsplit("_", 1)[1])
    return 0


__all__ = [
    "BLOCK_TYPES",
    "BLOCK_TYPE_LABELS",
    "DEFAULT_FAMILY_TYPES",
    "DEFAULT_SIZE_TYPES",
    "HEADING_TYPES",
    "MAX_HEADING_LEVEL",
    "heading_level",
    "heading_type",
]
