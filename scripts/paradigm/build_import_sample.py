"""Build the filled import sample of the ParadigmIT VAPT layout (deliverable 3b).

    python build_import_sample.py [OUTPUT.docx]

Default output: report_templates/generated/ParadigmIT_VAPT_Import_Sample.docx. The sample is
deliverable 1 (``build_word_template.build_document``) with another audit: client "Northwind
Traders", six findings chosen to exercise the importer (every severity, one without CVSS, one
with N/A references, one without screenshots, one with three screenshots and numbered lists,
one with HTML-special characters, one with two URLs). It doubles as a fill-in form: replace the
values, keep the labels. The importer tests (tests/docx_import/test_profile_paradigm.py,
tests/paradigm_template/test_import_roundtrip.py) read it and assert the values below
(``SAMPLE_FINDINGS`` and ``SAMPLE_META`` are imported by them).
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pd_findings  # noqa: E402
from build_word_template import build_document  # noqa: E402
from ox import Cell, Para, inline_picture, para, run  # noqa: E402
from pd_common import Ctx  # noqa: E402
from pd_meta import example_meta  # noqa: E402
from pd_order import order_content  # noqa: E402
from sample_import_data import SAMPLE_FINDINGS, SAMPLE_META  # noqa: E402

REPO = HERE.parent.parent
DEFAULT_OUT = REPO.parent / "report_templates" / "generated" / "ParadigmIT_VAPT_Import_Sample.docx"
POC_WIDTH = pd_findings.POC_WIDTH
POC_HEIGHT = round(POC_WIDTH * 560 / 1400, 2)


def _poc_cell(ctx: Ctx, item: dict) -> Cell:
    """PoC cell with 0..n screenshots, each followed by its caption, plus optional text."""
    paras = [para([run(text)], Para(style="TableValue")) for text in item.get("poc_text", ())]
    for index, (name, caption) in enumerate(item.get("images", ())):
        rid = ctx.image(name)
        label = f"PoC {item['id']}-{index + 1}"
        picture = inline_picture(rid, POC_WIDTH, POC_HEIGHT, label, caption)
        fmt = Para(align="center", before=3.3, line=1, line_rule="auto")
        paras.append(para([picture], fmt))
        paras.append(para([run(caption)], Para(style="Caption", before=1.4)))
    if not paras:
        paras.append(para([run("N/A")], Para(style="TableValue")))
    return Cell(paras)


def _register(findings: list[dict]) -> list[dict]:
    return [
        {
            "id": f["id"],
            "loc": [
                *(f["paths"] or ["N/A"]),
                f"Param: {f['parameter']}",
                f"Location: {f['location']}",
            ],
            "name": f["title"],
            "cwe": f["cwe"],
            "sev": f["severity"],
        }
        for f in findings
    ]


def sample_meta() -> dict:
    meta = example_meta()
    meta.update(copy.deepcopy(SAMPLE_META))
    return meta


def sample_content() -> dict:
    findings = copy.deepcopy(SAMPLE_FINDINGS)
    raw = {"note": "import sample", "findings": findings, "register": _register(findings)}
    return order_content(raw)  # Critical -> Informational by CVSS v3.1, renumbered 001..


def build_sample():
    original = pd_findings._poc_cell
    pd_findings._poc_cell = _poc_cell
    try:
        return build_document(sample_meta(), sample_content())
    finally:
        pd_findings._poc_cell = original


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = build_sample()
    doc.core_properties.comments = "ParadigmIT VAPT import sample (fill-in form)."
    doc.save(str(out))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
