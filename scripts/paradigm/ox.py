"""Small raw-OOXML builders (WordprocessingML + DrawingML) used by the Paradigm Word builder.

python-docx covers packages, parts and relationships; everything visual (run and paragraph
properties, tables, borders, shading, fields, inline and anchored drawings) is built here as
lxml elements. All geometry arguments are points; helpers convert to twips, half-points,
eighths of a point and EMU.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import count

from docx.oxml.ns import qn
from lxml import etree

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
}
EMU_PER_PT = 12700
_IDS = count(100)


def tw(pt: float) -> str:
    return str(round(pt * 20))


def emu(pt: float) -> str:
    return str(round(pt * EMU_PER_PT))


def _q(tag: str) -> str:
    prefix, name = tag.split(":")
    return f"{{{NS[prefix]}}}{name}"


def el(tag: str, attrs: dict[str, str] | None = None, *children: etree._Element) -> etree._Element:
    """Element ``tag`` ('w:p') with prefixed attribute names ('w:val') and children."""
    node = etree.Element(_q(tag), nsmap={tag.split(":")[0]: NS[tag.split(":")[0]]})
    for key, value in (attrs or {}).items():
        node.set(_q(key) if ":" in key else key, str(value))
    for child in children:
        if child is not None:
            node.append(child)
    return node


def wval(tag: str, value: object) -> etree._Element:
    return el(tag, {"w:val": str(value)})


# ---------------------------------------------------------------- runs


@dataclass(frozen=True)
class Font:
    """Character formatting. ``family`` None keeps the style's font."""

    family: str | None = None
    size: float | None = None
    color: str | None = None
    bold: bool | None = None
    italic: bool | None = None
    spacing: float | None = None  # letter-spacing in pt
    caps: bool = False
    position: float | None = None  # baseline shift in pt (positive raises)

    def with_(self, **changes: object) -> Font:
        values = {**self.__dict__, **changes}
        return Font(**values)  # type: ignore[arg-type]


def rpr(font: Font | None = None, style: str | None = None) -> etree._Element:
    node = el("w:rPr")
    if style:
        node.append(wval("w:rStyle", style))
    if font is None:
        return node
    if font.family:
        fam = font.family
        node.append(
            el("w:rFonts", {"w:ascii": fam, "w:hAnsi": fam, "w:cs": fam, "w:eastAsia": fam})
        )
    if font.bold is not None:
        node.append(el("w:b", {"w:val": "1" if font.bold else "0"}))
        node.append(el("w:bCs", {"w:val": "1" if font.bold else "0"}))
    if font.italic is not None:
        node.append(el("w:i", {"w:val": "1" if font.italic else "0"}))
    if font.caps:
        node.append(el("w:caps"))
    if font.color:
        node.append(wval("w:color", font.color.lstrip("#").upper()))
    if font.spacing:
        node.append(wval("w:spacing", tw(font.spacing)))
    if font.position:
        node.append(wval("w:position", round(font.position * 2)))
    if font.size:
        node.append(wval("w:sz", round(font.size * 2)))
        node.append(wval("w:szCs", round(font.size * 2)))
    return node


def text_el(text: str) -> etree._Element:
    node = el("w:t")
    node.text = text
    if text != text.strip() or "  " in text:
        node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return node


def run(text: str = "", font: Font | None = None, style: str | None = None) -> etree._Element:
    """A run; '\\t' becomes a tab and '\\n' a line break."""
    node = el("w:r", None, rpr(font, style))
    parts = text.split("\n")
    for index, line in enumerate(parts):
        if index:
            node.append(el("w:br"))
        chunks = line.split("\t")
        for cindex, chunk in enumerate(chunks):
            if cindex:
                node.append(el("w:tab"))
            if chunk:
                node.append(text_el(chunk))
    return node


def field_runs(instr: str, result: Sequence[etree._Element], font: Font | None = None) -> list:
    """Complex field: begin / instrText / separate / cached result runs / end."""
    instr_run = el("w:r", None, rpr(font))
    code = el("w:instrText")
    code.text = f" {instr} "
    code.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    instr_run.append(code)

    def char(kind: str) -> etree._Element:
        return el("w:r", None, rpr(font), el("w:fldChar", {"w:fldCharType": kind}))

    return [char("begin"), instr_run, char("separate"), *result, char("end")]


# ---------------------------------------------------------------- paragraphs


@dataclass(frozen=True)
class Para:
    """Paragraph formatting; distances in pt; ``line`` is exact line height unless rule given."""

    style: str | None = None
    align: str | None = None
    before: float | None = None
    after: float | None = None
    line: float | None = None
    line_rule: str = "exact"
    left: float | None = None
    right: float | None = None
    hanging: float | None = None
    first: float | None = None
    tabs: tuple[tuple[float, str], ...] = ()
    keep_next: bool = False
    keep_lines: bool = False
    page_break_before: bool | None = None
    shading: str | None = None
    num: tuple[int, int] | None = None
    outline: int | None = None
    widow: bool | None = None
    extra: tuple = field(default=())

    def with_(self, **changes: object) -> Para:
        return Para(**{**self.__dict__, **changes})  # type: ignore[arg-type]


def _spacing(fmt: Para) -> etree._Element | None:
    attrs: dict[str, str] = {}
    if fmt.before is not None:
        attrs["w:before"] = tw(fmt.before)
    if fmt.after is not None:
        attrs["w:after"] = tw(fmt.after)
    if fmt.line is not None:
        if fmt.line_rule == "auto":
            attrs["w:line"] = str(round(fmt.line * 240))
        else:
            attrs["w:line"] = tw(fmt.line)
        attrs["w:lineRule"] = fmt.line_rule
    return el("w:spacing", attrs) if attrs else None


def _indent(fmt: Para) -> etree._Element | None:
    attrs: dict[str, str] = {}
    if fmt.left is not None:
        attrs["w:left"] = tw(fmt.left)
    if fmt.right is not None:
        attrs["w:right"] = tw(fmt.right)
    if fmt.hanging is not None:
        attrs["w:hanging"] = tw(fmt.hanging)
    if fmt.first is not None:
        attrs["w:firstLine"] = tw(fmt.first)
    return el("w:ind", attrs) if attrs else None


def ppr(fmt: Para | None = None) -> etree._Element:
    """w:pPr children in schema order."""
    fmt = fmt or Para()
    node = el("w:pPr")
    if fmt.style:
        node.append(wval("w:pStyle", fmt.style))
    for flag, tag in ((fmt.keep_next, "w:keepNext"), (fmt.keep_lines, "w:keepLines")):
        if flag:
            node.append(el(tag))
    if fmt.page_break_before is not None:
        node.append(el("w:pageBreakBefore", {"w:val": "1" if fmt.page_break_before else "0"}))
    if fmt.widow is not None:
        node.append(wval("w:widowControl", "1" if fmt.widow else "0"))
    if fmt.num:
        node.append(el("w:numPr", None, wval("w:ilvl", fmt.num[1]), wval("w:numId", fmt.num[0])))
    if fmt.shading:
        node.append(shd(fmt.shading))
    if fmt.tabs:
        tabs = el("w:tabs")
        for pos, kind in fmt.tabs:
            tabs.append(el("w:tab", {"w:val": kind, "w:pos": tw(pos)}))
        node.append(tabs)
    for child in (_spacing(fmt), _indent(fmt)):
        if child is not None:
            node.append(child)
    if fmt.align:
        node.append(wval("w:jc", fmt.align))
    if fmt.outline is not None:
        node.append(wval("w:outlineLvl", fmt.outline))
    return node


def para(
    content: Iterable[etree._Element] | str = (), fmt: Para | None = None, font: Font | None = None
) -> etree._Element:
    """Paragraph from runs (or a plain string written with ``font``)."""
    node = el("w:p", None, ppr(fmt))
    items = [run(content, font)] if isinstance(content, str) else list(content)
    for item in items:
        node.append(item)
    return node


def shd(fill: str) -> etree._Element:
    return el("w:shd", {"w:val": "clear", "w:color": "auto", "w:fill": fill.lstrip("#").upper()})


# ---------------------------------------------------------------- tables

Border = tuple[float, str]  # (width pt, colour)
NONE_BORDER: Border = (0, "none")


def border_set(tag: str, sides: dict[str, Border]) -> etree._Element:
    node = el(tag)
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        if side not in sides:
            continue
        width, color = sides[side]
        if width <= 0 or color == "none":
            node.append(el(f"w:{side}", {"w:val": "nil"}))
            continue
        node.append(
            el(
                f"w:{side}",
                {
                    "w:val": "single",
                    "w:sz": str(round(width * 8)),
                    "w:space": "0",
                    "w:color": color.lstrip("#").upper(),
                },
            )
        )
    return node


def margins(
    tag: str, top: float = 0, left: float = 0, bottom: float = 0, right: float = 0
) -> etree._Element:
    node = el(tag)
    for side, value in (("top", top), ("left", left), ("bottom", bottom), ("right", right)):
        node.append(el(f"w:{side}", {"w:w": tw(value), "w:type": "dxa"}))
    return node


@dataclass
class Cell:
    content: list
    shading: str | None = None
    borders: dict[str, Border] | None = None
    pad: tuple[float, float, float, float] | None = None  # top, left, bottom, right
    valign: str | None = None
    span: int = 1
    vmerge: str | None = None  # 'restart' or 'continue'


@dataclass
class Row:
    cells: list[Cell]
    height: float | None = None
    rule: str = "exact"
    header: bool = False
    cant_split: bool = True


def _tc(cell: Cell, width: float) -> etree._Element:
    pr = el("w:tcPr", None, el("w:tcW", {"w:w": tw(width), "w:type": "dxa"}))
    if cell.span > 1:
        pr.append(wval("w:gridSpan", cell.span))
    if cell.vmerge:
        pr.append(wval("w:vMerge", cell.vmerge))
    if cell.borders:
        pr.append(border_set("w:tcBorders", cell.borders))
    if cell.shading:
        pr.append(shd(cell.shading))
    if cell.pad:
        top, left, bottom, right = cell.pad
        pr.append(margins("w:tcMar", top, left, bottom, right))
    if cell.valign:
        pr.append(wval("w:vAlign", cell.valign))
    node = el("w:tc", None, pr)
    for item in cell.content or [para()]:
        node.append(item)
    return node


def _tr(row: Row, widths: Sequence[float]) -> etree._Element:
    pr = el("w:trPr")
    if row.cant_split:
        pr.append(el("w:cantSplit"))
    if row.height:
        pr.append(el("w:trHeight", {"w:val": tw(row.height), "w:hRule": row.rule}))
    if row.header:
        pr.append(el("w:tblHeader"))
    node = el("w:tr", None, pr)
    col = 0
    for cell in row.cells:
        width = sum(widths[col : col + cell.span])
        node.append(_tc(cell, width))
        col += cell.span
    return node


def table(
    widths: Sequence[float],
    rows: Sequence[Row],
    *,
    indent: float = 0,
    borders: dict[str, Border] | None = None,
    pad: tuple[float, float, float, float] = (0, 0, 0, 0),
    style: str | None = None,
    align: str | None = None,
) -> etree._Element:
    """Fixed-layout table; ``widths`` in pt; default no borders and zero cell margins."""
    pr = el("w:tblPr")
    if style:
        pr.append(wval("w:tblStyle", style))
    pr.append(el("w:tblW", {"w:w": tw(sum(widths)), "w:type": "dxa"}))
    if align:
        pr.append(wval("w:jc", align))
    pr.append(el("w:tblInd", {"w:w": tw(indent), "w:type": "dxa"}))
    sides = {side: NONE_BORDER for side in ("top", "left", "bottom", "right", "insideH", "insideV")}
    pr.append(border_set("w:tblBorders", {**sides, **(borders or {})}))
    pr.append(el("w:tblLayout", {"w:type": "fixed"}))
    top, left, bottom, right = pad
    pr.append(margins("w:tblCellMar", top, left, bottom, right))
    pr.append(
        el(
            "w:tblLook",
            {
                "w:val": "0000",
                "w:firstRow": "0",
                "w:lastRow": "0",
                "w:firstColumn": "0",
                "w:lastColumn": "0",
                "w:noHBand": "1",
                "w:noVBand": "1",
            },
        )
    )
    grid = el("w:tblGrid")
    for width in widths:
        grid.append(el("w:gridCol", {"w:w": tw(width)}))
    node = el("w:tbl", None, pr, grid)
    for row in rows:
        node.append(_tr(row, widths))
    return node


# ---------------------------------------------------------------- drawings


def next_id() -> int:
    return next(_IDS)


def _graphic_pic(rid: str, w_pt: float, h_pt: float, name: str, pid: int) -> etree._Element:
    blip = el("a:blip", {"r:embed": rid})
    pic = el(
        "pic:pic",
        None,
        el(
            "pic:nvPicPr", None, el("pic:cNvPr", {"id": str(pid), "name": name}), el("pic:cNvPicPr")
        ),
        el("pic:blipFill", None, blip, el("a:stretch", None, el("a:fillRect"))),
        el(
            "pic:spPr",
            None,
            el(
                "a:xfrm",
                None,
                el("a:off", {"x": "0", "y": "0"}),
                el("a:ext", {"cx": emu(w_pt), "cy": emu(h_pt)}),
            ),
            el("a:prstGeom", {"prst": "rect"}, el("a:avLst")),
        ),
    )
    data = el("a:graphicData", {"uri": NS["pic"]}, pic)
    return el("a:graphic", None, data)


def _doc_pr(pid: int, name: str, descr: str) -> etree._Element:
    return el("wp:docPr", {"id": str(pid), "name": name, "descr": descr})


def inline_picture(
    rid: str, w_pt: float, h_pt: float, name: str, descr: str = "", font_size: float = 1
) -> etree._Element:
    """A run holding an inline picture (tiny run font so the line is the picture height)."""
    pid = next_id()
    inline = el(
        "wp:inline",
        {"distT": "0", "distB": "0", "distL": "0", "distR": "0"},
        el("wp:extent", {"cx": emu(w_pt), "cy": emu(h_pt)}),
        el("wp:effectExtent", {"l": "0", "t": "0", "r": "0", "b": "0"}),
        _doc_pr(pid, name, descr or name),
        el("wp:cNvGraphicFramePr", None, el("a:graphicFrameLocks", {"noChangeAspect": "1"})),
        _graphic_pic(rid, w_pt, h_pt, name, pid),
    )
    return el("w:r", None, rpr(Font(size=font_size)), el("w:drawing", None, inline))


@dataclass(frozen=True)
class Box:
    """Anchored box: position relative to the page (or ``rel``) in pt."""

    x: float
    y: float
    w: float
    h: float
    rel_h: str = "page"
    rel_v: str = "page"
    behind: bool = True


def _anchor(box: Box, name: str, descr: str, graphic: etree._Element, z: int) -> etree._Element:
    pid = next_id()
    return el(
        "wp:anchor",
        {
            "distT": "0",
            "distB": "0",
            "distL": "0",
            "distR": "0",
            "simplePos": "0",
            "relativeHeight": str(z),
            "behindDoc": "1" if box.behind else "0",
            "locked": "1",
            "layoutInCell": "1",
            "allowOverlap": "1",
        },
        el("wp:simplePos", {"x": "0", "y": "0"}),
        el("wp:positionH", {"relativeFrom": box.rel_h}, _pos(box.x)),
        el("wp:positionV", {"relativeFrom": box.rel_v}, _pos(box.y)),
        el("wp:extent", {"cx": emu(box.w), "cy": emu(box.h)}),
        el("wp:effectExtent", {"l": "0", "t": "0", "r": "0", "b": "0"}),
        el("wp:wrapNone"),
        _doc_pr(pid, name, descr or name),
        el("wp:cNvGraphicFramePr"),
        graphic,
    )


def _pos(value: float) -> etree._Element:
    node = el("wp:posOffset")
    node.text = emu(value)
    return node


def anchored_picture(
    rid: str, box: Box, name: str, descr: str = "", z: int = 1000
) -> etree._Element:
    graphic = _graphic_pic(rid, box.w, box.h, name, next_id())
    return el("w:r", None, el("w:drawing", None, _anchor(box, name, descr, graphic, z)))


def _fill(color: str | None, alpha: float | None = None) -> etree._Element:
    if color is None:
        return el("a:noFill")
    clr = el("a:srgbClr", {"val": color.lstrip("#").upper()})
    if alpha is not None:
        clr.append(el("a:alpha", {"val": str(round(alpha * 100000))}))
    return el("a:solidFill", None, clr)


@dataclass(frozen=True)
class ShapeStyle:
    prst: str = "rect"
    fill: str | None = None
    fill_alpha: float | None = None
    line: str | None = None
    line_w: float = 0
    line_alpha: float | None = None


def _shape_graphic(box: Box, style: ShapeStyle) -> etree._Element:
    line = el(
        "a:ln",
        {"w": emu(style.line_w)} if style.line else {},
        _fill(style.line, style.line_alpha) if style.line else el("a:noFill"),
    )
    sp_pr = el(
        "wps:spPr",
        None,
        el(
            "a:xfrm",
            None,
            el("a:off", {"x": "0", "y": "0"}),
            el("a:ext", {"cx": emu(box.w), "cy": emu(box.h)}),
        ),
        el("a:prstGeom", {"prst": style.prst}, el("a:avLst")),
        _fill(style.fill, style.fill_alpha),
        line,
    )
    body = el("wps:bodyPr", {"lIns": "0", "tIns": "0", "rIns": "0", "bIns": "0"})
    wsp = el("wps:wsp", None, el("wps:cNvSpPr"), sp_pr, body)
    return el("a:graphic", None, el("a:graphicData", {"uri": NS["wps"]}, wsp))


def anchored_shape(
    box: Box, style: ShapeStyle, name: str = "Shape", z: int = 900
) -> etree._Element:
    """A run holding a floating DrawingML shape (rect / ellipse ...)."""
    anchor = _anchor(box, name, name, _shape_graphic(box, style), z)
    return el("w:r", None, el("w:drawing", None, anchor))


def inline_shape(w_pt: float, h_pt: float, style: ShapeStyle, name: str = "Dot") -> etree._Element:
    pid = next_id()
    inline = el(
        "wp:inline",
        {"distT": "0", "distB": "0", "distL": "0", "distR": "0"},
        el("wp:extent", {"cx": emu(w_pt), "cy": emu(h_pt)}),
        el("wp:effectExtent", {"l": "0", "t": "0", "r": "0", "b": "0"}),
        _doc_pr(pid, name, name),
        el("wp:cNvGraphicFramePr"),
        _shape_graphic(Box(0, 0, w_pt, h_pt), style),
    )
    return el("w:r", None, el("w:drawing", None, inline))


def set_attr(node: etree._Element, name: str, value: str) -> None:
    node.set(qn(name), value)
