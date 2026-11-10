"""Design tokens, Word styles and numbering definitions of the ParadigmIT VAPT report.

Every text role of the PDF (see the design spec: type-scale.md / palette.md) is a named Word
style, so the PwnDoc template and users editing the report reuse the same look. Fonts use the
installed family names: 'Lato', 'Lato Medium', 'Lato Heavy', 'Lato Black', 'Carlito' and
'Noto Serif' (the heavier Lato weights are separate families in Word and LibreOffice).
"""

from __future__ import annotations

from dataclasses import dataclass

from docx.document import Document
from lxml import etree
from ox import Font, Para, el, ppr, rpr, tw, wval

LATO, LATO_MED, LATO_HEAVY, LATO_BLACK = "Lato", "Lato Medium", "Lato Heavy", "Lato Black"
CARLITO, NOTO_SERIF = "Carlito", "Noto Serif"

NAVY, BANNER, CARD_NAVY = "071724", "0B2234", "11293A"
BLUE, TEAL, MINT, HEAD_BLUE = "086AD8", "00A99B", "04D9A8", "0879D3"
INK, INK2, INK3 = "152535", "526779", "8394A4"
BORDER, LABEL_FILL, ALT_ROW = "C9D4DF", "F3F6F9", "F6F9FC"
PANEL, CARD2, CRED_PANEL = "F8FAFC", "F3F7FA", "F4F8FA"

# Family A (designed Lato pages): bars, dots, labels, accent strips.
SEV_A = {
    "Critical": "C93446",
    "High": "EA7B3E",
    "Medium": "DEA928",
    "Low": "1D83C1",
    "Informational": "8797A4",
    "Info": "8797A4",
}
# Family B (Carlito pages): (text colour, cell tint).
SEV_B = {
    "Critical": ("C00000", "FBE5E5"),
    "High": ("ED7D31", "FCEBDD"),
    "Medium": ("BF9000", "FBF4DD"),
    "Low": ("2E75B6", "E8F0F9"),
    "Info": ("2AA5C9", "E3F4FA"),
    "Informational": ("2AA5C9", "E3F4FA"),
}

STEP_ABSTRACT, BULLET_ABSTRACT = 40, 41


@dataclass(frozen=True)
class StyleDef:
    sid: str
    name: str
    font: Font
    para: Para | None = None
    kind: str = "paragraph"
    based: str | None = "Normal"
    next_style: str | None = None


def _p(**kw: object) -> Para:
    return Para(**kw)  # type: ignore[arg-type]


DESIGNED_STYLES: tuple[StyleDef, ...] = (
    StyleDef("CoverKicker", "Cover Kicker", Font(LATO_HEAVY, 10, MINT), _p(line=12)),
    StyleDef("Title", "Title", Font(LATO_BLACK, 38, "FFFFFF"), _p(line=46)),
    StyleDef("Subtitle", "Subtitle", Font(LATO, 24, "FFFFFF"), _p(line=29)),
    StyleDef("CoverMeta", "Cover Meta", Font(LATO_HEAVY, 9.5, "80C4CE"), _p(line=12)),
    StyleDef("CoverCardLabel", "Cover Card Label", Font(LATO_HEAVY, 8.5, "7EAFB8"), _p(line=11)),
    StyleDef("CoverCardValue", "Cover Card Value", Font(LATO, 16, "FFFFFF"), _p(line=20)),
    StyleDef(
        "CoverFooter",
        "Cover Footer",
        Font(LATO_HEAVY, 8.5, "82ACB6"),
        _p(line=11, tabs=((566.9 - 46, "right"),)),
    ),
    StyleDef(
        "Heading1",
        "heading 1",
        Font(LATO_BLACK, 29, INK, bold=False),
        _p(line=36, keep_next=True, page_break_before=True, outline=0),
        next_style="Kicker",
    ),
    StyleDef(
        "Heading2",
        "heading 2",
        Font(LATO, 13, INK, bold=False),
        _p(line=16, keep_next=True, outline=1, tabs=((31, "left"),)),
    ),
    StyleDef("Kicker", "Kicker", Font(LATO_HEAVY, 9, BLUE), _p(line=11, keep_next=True)),
    StyleDef("Lead", "Lead", Font(LATO, 10, INK2), _p(line=14)),
    StyleDef("BodyText", "Body Text", Font(LATO, 10, INK), _p(line=13.4)),
    StyleDef("CardLabel", "Card Label", Font(LATO_HEAVY, 8, INK3), _p(line=10)),
    StyleDef("CardValue", "Card Value", Font(LATO, 11, INK), _p(line=14)),
    StyleDef("IndexLabel", "Index Label", Font(LATO_BLACK, 9, BLUE), _p(line=11)),
    StyleDef(
        "TOC1",
        "toc 1",
        Font(LATO, 12, INK),
        _p(line=15, tabs=((34, "left"), (519.8, "right")), left=3),
    ),
    StyleDef(
        "TOC2", "toc 2", Font(LATO, 9.5, INK2), _p(line=12, tabs=((521.3, "right"),), left=34)
    ),
    StyleDef("Chip", "Chip", Font(LATO_HEAVY, 7.5, "0E8064"), _p(line=19, align="center")),
    StyleDef(
        "ClosingTitle",
        "Closing Title",
        Font(NOTO_SERIF, 37, "112639", bold=True),
        _p(line=45, align="center", page_break_before=True),
    ),
    StyleDef(
        "ClosingSubtitle",
        "Closing Subtitle",
        Font(NOTO_SERIF, 18, "00908A"),
        _p(line=22, align="center"),
    ),
)


LEGACY_STYLES: tuple[StyleDef, ...] = (
    StyleDef(
        "RegisterHeading",
        "Register Heading",
        Font(NOTO_SERIF, 16, "0F68B4", bold=True),
        _p(line=21, keep_next=True, outline=0, left=11.1),
        based="Normal",
    ),
    StyleDef(
        "RegisterIntro",
        "Register Intro",
        Font(NOTO_SERIF, 10, "4A5E73"),
        _p(line=16.9, left=11.1),
    ),
    StyleDef(
        "SectionKicker",
        "Section Kicker",
        Font(CARLITO, 14.5, "106DBA", bold=True),
        _p(line=18, keep_next=True, outline=0, left=11.1),
    ),
    StyleDef("LegacyBody", "Legacy Body", Font(CARLITO, 9, "3E5264"), _p(line=11, left=11.1)),
    StyleDef(
        "Heading3",
        "heading 3",
        Font(CARLITO, 11, HEAD_BLUE, bold=True),
        _p(line=13.4, keep_next=True, outline=2, left=11.1),
    ),
    StyleDef("RegisterHead", "Register Head", Font(CARLITO, 8.5, "FFFFFF", bold=True), _p(line=11)),
    StyleDef("RegisterText", "Register Text", Font(CARLITO, 8, "0D2338"), _p(line=13.5)),
    StyleDef("TableLabel", "Table Label", Font(CARLITO, 9, INK, bold=True), _p(line=12.1)),
    StyleDef("TableValue", "Table Value", Font(CARLITO, 9, INK), _p(line=12.1)),
    StyleDef("TableURL", "Table URL", Font(CARLITO, 8, INK), _p(line=11.2)),
    StyleDef(
        "TableList",
        "Table List",
        Font(CARLITO, 9, INK),
        _p(line=12.65, after=2),
        based="TableValue",
    ),
    StyleDef(
        "Caption",
        "caption",
        Font(CARLITO, 7.5, "5A6B7B", italic=True, bold=False),
        _p(line=9.2, align="center"),
    ),
    StyleDef(
        "Disclaimer",
        "Disclaimer",
        Font(CARLITO, 10, "3E5264"),
        _p(line=16.1, after=10, left=11.1, right=16.4),
    ),
    StyleDef("Header", "header", Font(LATO_HEAVY, 8.5, INK2), _p(line=10.2)),
    StyleDef("Footer", "footer", Font(LATO, 7, INK3), _p(line=10, tabs=((526, "right"),))),
)


def _char_styles() -> list[StyleDef]:
    styles = [
        StyleDef("H2Number", "H2 Number", Font(LATO_BLACK, 9, BLUE), kind="character", based=None),
        StyleDef(
            "IndexNumber", "Index Number", Font(LATO_BLACK, 9.5, TEAL), kind="character", based=None
        ),
        StyleDef("IndexPage", "Index Page", Font(LATO, 11, BLUE), kind="character", based=None),
        StyleDef("PageNumber", "page number", Font(LATO, 8, INK2), kind="character", based=None),
    ]
    for sev, (color, _tint) in SEV_B.items():
        if sev == "Informational":
            continue
        styles.append(
            StyleDef(
                f"Severity{sev}",
                f"Severity {sev}",
                Font(CARLITO, 9, color, bold=True),
                kind="character",
                based=None,
            )
        )
    return styles


def all_styles() -> list[StyleDef]:
    return [*DESIGNED_STYLES, *LEGACY_STYLES, *_char_styles()]


def _style_el(sdef: StyleDef) -> etree._Element:
    node = el(
        "w:style",
        {
            "w:type": sdef.kind,
            "w:styleId": sdef.sid,
            "w:customStyle": "0" if sdef.sid in BUILTIN else "1",
        },
    )
    node.append(wval("w:name", sdef.name))
    if sdef.based:
        node.append(wval("w:basedOn", sdef.based))
    if sdef.next_style:
        node.append(wval("w:next", sdef.next_style))
    node.append(el("w:qFormat"))
    if sdef.kind == "paragraph":
        node.append(ppr(sdef.para or Para()))
    node.append(rpr(sdef.font))
    return node


BUILTIN = {
    "Title",
    "Subtitle",
    "Heading1",
    "Heading2",
    "Heading3",
    "TOC1",
    "TOC2",
    "Caption",
    "Header",
    "Footer",
    "PageNumber",
    "BodyText",
}


def _doc_defaults(styles: etree._Element) -> None:
    defaults = styles.find(f"{{{styles.nsmap['w']}}}docDefaults")
    if defaults is not None:
        styles.remove(defaults)
    run_defaults = el("w:rPrDefault", None, rpr(Font(LATO, 10, INK)))
    run_defaults[0].append(
        el("w:lang", {"w:val": "en-GB", "w:eastAsia": "en-US", "w:bidi": "ar-SA"})
    )
    para_defaults = el(
        "w:pPrDefault",
        None,
        el("w:pPr", None, el("w:spacing", {"w:after": "0", "w:line": "240", "w:lineRule": "auto"})),
    )
    styles.insert(0, el("w:docDefaults", None, run_defaults, para_defaults))


def install_styles(doc: Document) -> None:
    styles = doc.styles.element
    _doc_defaults(styles)
    wanted = {sdef.sid for sdef in all_styles()}
    w_ns = styles.nsmap["w"]
    for node in list(styles.findall(f"{{{w_ns}}}style")):
        if node.get(f"{{{w_ns}}}styleId") in wanted:
            styles.remove(node)
    normal = _find_style(styles, "Normal")
    if normal is not None:
        for child in list(normal):
            if child.tag in (f"{{{w_ns}}}rPr", f"{{{w_ns}}}pPr"):
                normal.remove(child)
    for sdef in all_styles():
        styles.append(_style_el(sdef))


def _find_style(styles: etree._Element, sid: str) -> etree._Element | None:
    w_ns = styles.nsmap["w"]
    for node in styles.findall(f"{{{w_ns}}}style"):
        if node.get(f"{{{w_ns}}}styleId") == sid:
            return node
    return None


# ---------------------------------------------------------------- numbering


def _level(
    ilvl: int, fmt: str, text: str, font: Font, left: float, hanging: float, tab: float | None
) -> etree._Element:
    pr = el("w:pPr")
    if tab is not None:
        pr.append(el("w:tabs", None, el("w:tab", {"w:val": "num", "w:pos": tw(tab)})))
    pr.append(el("w:ind", {"w:left": tw(left), "w:hanging": tw(hanging)}))
    return el(
        "w:lvl",
        {"w:ilvl": str(ilvl)},
        wval("w:start", 1),
        wval("w:numFmt", fmt),
        wval("w:suff", "tab"),
        wval("w:lvlText", text),
        wval("w:lvlJc", "left"),
        pr,
        rpr(font),
    )


def _abstract(aid: int, name: str, level: etree._Element) -> etree._Element:
    return el(
        "w:abstractNum",
        {"w:abstractNumId": str(aid)},
        wval("w:multiLevelType", "singleLevel"),
        wval("w:name", name),
        level,
    )


class Numbering:
    """Owns numbering.xml: the step list and bullet definitions plus one w:num per list."""

    def __init__(self, doc: Document) -> None:
        self.root = doc.part.numbering_part.element
        w_ns = NSW
        for node in list(self.root):
            if node.tag in (f"{{{w_ns}}}abstractNum", f"{{{w_ns}}}num"):
                self.root.remove(node)
        steps = _level(0, "decimal", "%1.", Font(CARLITO, 9, INK, bold=True), 16.5, 15, 12.5)
        bullet = _level(0, "bullet", "●", Font(LATO, 5.5, TEAL), 11.1, 11.1, None)
        self.root.append(_abstract(STEP_ABSTRACT, "Paradigm steps", steps))
        self.root.append(_abstract(BULLET_ABSTRACT, "Paradigm bullets", bullet))
        self._next = 1

    def new_list(self, abstract: int = STEP_ABSTRACT) -> int:
        num_id = self._next
        self._next += 1
        override = el("w:lvlOverride", {"w:ilvl": "0"}, wval("w:startOverride", 1))
        self.root.append(
            el("w:num", {"w:numId": str(num_id)}, wval("w:abstractNumId", abstract), override)
        )
        return num_id


NSW = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
