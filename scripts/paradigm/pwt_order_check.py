# ruff: noqa: S603, S607  (fixed local tool: pdftotext)
"""Check a PwnDoc render of the ParadigmIT template against docs/FINDING_ORDER.md part A.

    python pwt_order_check.py AUDIT.json RENDER.docx [RENDER.pdf]

From the audit JSON (the stored PwnDoc data, any order) it computes the expected report order
(CVSS v3.1 score desc inside Critical -> Informational, ties by identifier, seq 001..), then
checks the rendered DOCX: register rows (S. No. + title, same order), 'Finding NNN - title'
headings, the detail-table Severity cell (text = derived band, fill = that band's tint) and,
with the PDF, the page-3 distribution counts. Prints a JSON report; exit 1 on any failure.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # noqa: S404  (fixed local tool: pdftotext)
import sys
import zipfile
from collections import Counter
from pathlib import Path

from lxml import etree

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from pd_order import BANDS, band_of_label, band_of_score, numeric_score  # noqa: E402
from pd_styles import SEV_B  # noqa: E402

from pwndoc_ai.cvss import parse_vector  # noqa: E402

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
HEADING = re.compile(r"^Finding (\d{3,}) — (.*)$")
LEGEND = ("Critical", "High", "Medium", "Low", "Informational")


def _custom(finding: dict, label: str) -> object:
    for field in finding.get("customFields", []):
        if field["customField"]["label"].lower() == label.lower():
            return field.get("text")
    return None


def finding_key(finding: dict, position: int) -> tuple:
    vector = finding.get("cvssv3") or ""
    parsed = parse_vector(vector)
    v4 = isinstance(vector, str) and vector.strip().upper().startswith("CVSS:4")
    score = parsed.score if parsed else None
    if score is None and not v4:
        score = numeric_score(_custom(finding, "CVSSscore"))
    band = (
        band_of_score(score) if score is not None else band_of_label(_custom(finding, "Severity"))
    )
    index = finding.get("identifier", position)
    return (BANDS.index(band), score is None, -(score or 0.0), index, position), band


def expected_order(audit: dict) -> list[dict]:
    findings = audit.get("findings", [])
    keyed = sorted((finding_key(f, i), f) for i, f in enumerate(findings))
    width = max(3, len(str(len(keyed))))
    return [
        {"seq": str(n).zfill(width), "title": f["title"], "band": band}
        for n, ((_key, band), f) in enumerate(keyed, 1)
    ]


def _text(node: etree._Element) -> str:
    return "".join(t.text or "" for t in node.iter(f"{W}t"))


def _fill(cell: etree._Element) -> str | None:
    shd = cell.find(f"{W}tcPr/{W}shd")
    return shd.get(f"{W}fill") if shd is not None else None


def docx_view(docx: Path) -> dict:
    """Headings, register rows and detail-table severity cells of a rendered report."""
    with zipfile.ZipFile(docx) as package:
        root = etree.fromstring(package.read("word/document.xml"))
    headings = [m.groups() for p in root.iter(f"{W}p") if (m := HEADING.match(_text(p)))]
    register, severities = [], []
    for tbl in root.iter(f"{W}tbl"):
        for tr in tbl.findall(f"{W}tr"):
            cells = tr.findall(f"{W}tc")
            texts = [_text(c) for c in cells]
            if len(cells) == 5 and re.fullmatch(r"\d{3,}", texts[0]):
                register.append((texts[0], texts[2], texts[4], _fill(cells[4])))
            if len(cells) == 2 and texts[0] == "Severity":
                severities.append((texts[1], _fill(cells[1])))
    return {"headings": headings, "register": register, "severities": severities}


def _tint(band: str) -> str | None:
    key = "Info" if band == "Informational" else band
    return SEV_B[key][1].lstrip("#").upper() if key in SEV_B else None


def check_docx(expected: list[dict], view: dict) -> list[str]:
    want = [(e["seq"], e["title"]) for e in expected]
    problems = []
    if view["headings"] != want:
        problems.append(f"headings {view['headings']} != {want}")
    if [(r[0], r[1]) for r in view["register"]] != want:
        problems.append("register order/numbering differs from the expected order")
    for e, (text, fill) in zip(expected, view["severities"], strict=False):
        shown = "N/A" if e["band"] == "Unknown" else e["band"]
        if text != shown or (fill or "").upper() != (_tint(e["band"]) or "").upper():
            problems.append(f"{e['seq']} severity cell {text}/{fill}, expected {shown}")
    if len(view["severities"]) != len(expected):
        problems.append(f"{len(view['severities'])} severity rows for {len(expected)} findings")
    return problems


def check_counts(expected: list[dict], pdf: Path) -> list[str]:
    if not shutil.which("pdftotext"):
        return []
    page = subprocess.run(  # noqa: S603, S607
        ["pdftotext", "-f", "3", "-l", "3", str(pdf), "-"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    counts = Counter(e["band"] for e in expected)
    tail = page.split("Informational", 1)[-1].split()
    numbers = [t for t in tail if re.fullmatch(r"\d{2}", t)][:5]
    want = [f"{counts[b]:02d}" for b in LEGEND]
    return [] if numbers == want else [f"legend counts {numbers} != {want}"]


def run(audit_path: Path, docx: Path, pdf: Path | None = None) -> dict:
    expected = expected_order(json.loads(audit_path.read_text(encoding="utf-8")))
    problems = check_docx(expected, docx_view(docx))
    if pdf is not None and expected:
        problems += check_counts(expected, pdf)
    return {"findings": len(expected), "order": expected, "problems": problems}


def main(argv: list[str]) -> int:
    pdf = Path(argv[3]) if len(argv) > 3 else None
    report = run(Path(argv[1]), Path(argv[2]), pdf)
    print(json.dumps({k: v for k, v in report.items() if k != "order"}, indent=1))
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
