"""Build the ParadigmIT VAPT PwnDoc report template (deliverable 2) from deliverable 1.

    python build_pwndoc_template.py [OUTPUT.docx]

The Word report generator (build_word_template.build_document) runs with the tag vocabulary of
pwt_tags as its meta and the per-finding builders of pwt_blocks patched in, so every style,
font, size, colour and spacing is the deliverable-1 one. Post-processing then:
  * empties the cached page numbers of the index TOC field and marks it dirty (Word updates it);
  * lets rows holding tags grow (exact heights become at-least);
  * reserves numId 1 (bullets) and 2 (decimal) for PwnDoc's html2ooxml lists;
  * writes the companion custom filter file (report-filters-custom.js) next to the template.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build_word_template as bwt  # noqa: E402
import pd_hdrftr  # noqa: E402
import pwt_blocks  # noqa: E402
from docx.document import Document as DocumentType  # noqa: E402
from lxml import etree  # noqa: E402
from ox import Font, el, run, wval  # noqa: E402
from pd_styles import BULLET_ABSTRACT, INK2, LATO_HEAVY, STEP_ABSTRACT  # noqa: E402
from pwt_tags import FOOTER_B, tag_meta  # noqa: E402

REPO = HERE.parent.parent
GENERATED = REPO.parent / "report_templates" / "generated"
DEFAULT_OUT = GENERATED / "ParadigmIT_VAPT_PwnDoc_Template.docx"
FILTER_SRC = HERE / "pwndoc_custom" / "report-filters-custom.js"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
TAG_CONTENT = {"findings": [{"id": "000"}], "register": [{}]}
RESERVED_NUM = {1: BULLET_ABSTRACT, 2: STEP_ABSTRACT}
NUM_SHIFT = 100


def client_logo_tag() -> list[etree._Element]:
    """Raw docxtemplater-image-module-pwndoc tag, replacing the literal placeholder text.

    PwnDoc's bundled Template1.docx embeds ``{%company.logo_small}`` as plain text runs
    inside the header paragraph (confirmed by inspecting its header2.xml) -- the image module
    matches the tag text itself and substitutes an inline picture in place of the run, there is
    no drawing/anchor wrapper around the tag. ``company.logo_small`` is scaled by the server to
    a 37 pt-tall logo (report-generator.js's opts.getSize), matching the header's row height.
    When the company has no logo set, report-generator.js sets the field to the string
    "undefined" and the image module renders nothing in its place (no crash either way).
    """
    return [run("{%company.logo_small}", Font(LATO_HEAVY, 8.5, INK2))]


def build_template() -> DocumentType:
    variant_b = dataclasses.replace(bwt.VARIANT_B, text=FOOTER_B)
    saved = (bwt.VARIANT_B, bwt.finding, pd_hdrftr.default_client_logo)
    try:
        bwt.VARIANT_B, bwt.finding = variant_b, pwt_blocks.finding
        pd_hdrftr.default_client_logo = client_logo_tag
        with pwt_blocks.patched_builders():
            doc = bwt.build_document(meta=tag_meta(), content=TAG_CONTENT)
    finally:
        bwt.VARIANT_B, bwt.finding, pd_hdrftr.default_client_logo = saved
    root = doc.element
    clear_toc_pages(root)
    grow_tag_rows(root)
    reserve_numbering(doc)
    doc.core_properties.comments = "PwnDoc report template (ParadigmIT VAPT), see guide."
    return doc


def _texts(node: etree._Element) -> str:
    return "".join(node.itertext())


def clear_toc_pages(root: etree._Element) -> None:
    """Index TOC: drop the stored page numbers (static text) and ask Word to update the field."""
    for begin in root.iter(f"{W}fldChar"):
        if begin.get(f"{W}fldCharType") == "begin":
            instr = begin.getparent().getnext()
            if instr is not None and "TOC" in _texts(instr):
                begin.set(f"{W}dirty", "true")
    for p in root.iter(f"{W}p"):
        style = p.find(f"{W}pPr/{W}pStyle")
        if style is None or style.get(f"{W}val") not in ("TOC1", "TOC2"):
            continue
        runs = [r for r in p.findall(f"{W}r") if r.find(f"{W}t") is not None]
        if runs and runs[-1].findtext(f"{W}t", "").strip().isdigit():
            p.remove(runs[-1])


COUNT_TAG = re.compile(r"\{findings \| bandCount:'[^']*'[^}]*\}")


def grow_tag_rows(root: etree._Element) -> None:
    """Rows holding text tags get 'atLeast' heights (exact rows would clip long values).

    Rows whose only tags are finding counts (2-digit numbers) keep their exact height: their
    content is laid out against the clip, like in deliverable 1.
    """
    for tr in root.iter(f"{W}tr"):
        height = tr.find(f"{W}trPr/{W}trHeight")
        if height is None or tr.find(f"{W}trPr/{W}tblHeader") is not None:
            continue
        if "{" in COUNT_TAG.sub("", _texts(tr)):
            height.set(f"{W}hRule", "atLeast")


def reserve_numbering(doc: DocumentType) -> None:
    """html2ooxml writes numId 1 for bullets and 2 for ordered lists: give them those lists."""
    numbering = doc.part.numbering_part.element
    for num in numbering.findall(f"{W}num"):
        num.set(f"{W}numId", str(int(num.get(f"{W}numId")) + NUM_SHIFT))
    for part_root in (doc.element, doc.styles.element):
        for num_id in part_root.iter(f"{W}numId"):
            num_id.set(f"{W}val", str(int(num_id.get(f"{W}val")) + NUM_SHIFT))
    first = numbering.find(f"{W}num")
    for num_id, abstract in sorted(RESERVED_NUM.items(), reverse=True):
        node = el("w:num", {"w:numId": str(num_id)}, wval("w:abstractNumId", abstract))
        if first is not None:
            first.addprevious(node)
            first = node
        else:
            numbering.append(node)


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    build_template().save(str(out))
    filt = out.parent / "pwndoc_custom" / "report-filters-custom.js"
    filt.parent.mkdir(exist_ok=True)
    filt.write_text(FILTER_SRC.read_text(encoding="utf-8"), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
