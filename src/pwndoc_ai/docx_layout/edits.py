"""Small structural edits of WordprocessingML. They only add, switch or remove layout properties
and whole empty paragraphs; text, runs with content and every attribute they do not own stay."""

from __future__ import annotations

from lxml import etree

from .ooxml import OFF_VALUES, local, w

# CT_PPrBase sequence: a new property goes before the first existing sibling that follows it.
PPR_ORDER = (
    "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr",
    "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap",
    "overflowPunct", "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd",
    "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc",
    "textDirection", "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr",
    "sectPr", "pPrChange",
)  # fmt: skip
# CT_Settings: updateFields goes before the first of these that exists.
SETTINGS_AFTER_UPDATE_FIELDS = (
    "hdrShapeDefaults", "footnotePr", "endnotePr", "compat", "docVars", "rsids", "mathPr",
    "attachedSchema", "themeFontLang", "clrSchemeMapping", "doNotIncludeSubdocsInStats",
    "doNotAutoCompressPictures", "forceUpgrade", "captions", "readModeInkLockDown", "smartTagType",
    "schemaLibrary", "shapeDefaults", "doNotEmbedSmartTags", "decimalSymbol", "listSeparator",
)  # fmt: skip
RUN_KEEP = frozenset({"rPr"})


def _ppr(paragraph: etree._Element) -> etree._Element:
    ppr = paragraph.find(w("pPr"))
    if ppr is None:
        ppr = etree.Element(w("pPr"))
        paragraph.insert(0, ppr)
    return ppr


def _insert_ordered(parent: etree._Element, child: etree._Element, order: tuple[str, ...]) -> None:
    rank = order.index(local(child))
    for index, sibling in enumerate(parent):
        name = local(sibling)
        if name in order and order.index(name) > rank:
            parent.insert(index, child)
            return
    parent.append(child)


def switch_on(paragraph: etree._Element, prop: str) -> bool:
    """Make the on/off paragraph property ``prop`` effective. True when the XML changed."""
    ppr = _ppr(paragraph)
    node = ppr.find(w(prop))
    if node is None:
        _insert_ordered(ppr, etree.Element(w(prop)), PPR_ORDER)
        return True
    value = node.get(w("val"))
    if value is not None and value.lower() in OFF_VALUES:
        del node.attrib[w("val")]
        return True
    return False


def switch_off(paragraph: etree._Element, prop: str) -> None:
    ppr = _ppr(paragraph)
    node = ppr.find(w(prop))
    if node is None:
        node = etree.Element(w(prop))
        _insert_ordered(ppr, node, PPR_ORDER)
    node.set(w("val"), "0")


def remove_page_breaks(paragraph: etree._Element) -> int:
    """Delete the hard page breaks of the paragraph's own runs (and runs left with nothing)."""
    removed = 0
    for run in paragraph.findall(w("r")):
        for node in run.findall(w("br")):
            if node.get(w("type")) == "page":
                run.remove(node)
                removed += 1
        if all(local(child) in RUN_KEEP for child in run):
            paragraph.remove(run)
    return removed


def remove_element(element: etree._Element) -> None:
    parent = element.getparent()
    if parent is not None:
        parent.remove(element)


def protect_rows(table: etree._Element) -> int:
    """Add cantSplit to the rows of ``table`` (not of nested tables) that lack it."""
    changed = 0
    for row in table.findall(w("tr")):
        trpr = row.find(w("trPr"))
        if trpr is None:
            trpr = etree.Element(w("trPr"))
            exceptions = row.find(w("tblPrEx"))
            row.insert(row.index(exceptions) + 1 if exceptions is not None else 0, trpr)
        if trpr.find(w("cantSplit")) is None:
            trpr.append(etree.Element(w("cantSplit")))
            changed += 1
    return changed


def set_update_fields(settings: etree._Element) -> bool:
    """Ask Word to refresh fields (the TOC) on open. True when the XML changed."""
    node = settings.find(w("updateFields"))
    if node is None:
        node = etree.Element(w("updateFields"))
        node.set(w("val"), "true")
        for index, sibling in enumerate(settings):
            if local(sibling) in SETTINGS_AFTER_UPDATE_FIELDS:
                settings.insert(index, node)
                return True
        settings.append(node)
        return True
    if (node.get(w("val")) or "true").lower() in OFF_VALUES:
        node.set(w("val"), "true")
        return True
    return False
