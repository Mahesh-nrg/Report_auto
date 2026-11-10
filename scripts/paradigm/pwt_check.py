# ruff: noqa: S603, S607  (fixed local tool: pdftotext)
"""Check rendered PwnDoc outputs of the ParadigmIT template (docx + its LibreOffice PDF).

    python pwt_check.py RENDER_DIR [--source SOURCE.pdf] [--json OUT.json]

Per sample NAME (NAME.docx + NAME.pdf in RENDER_DIR): leftover template tags in the docx text,
literal 'undefined', blank PDF pages, the repo layout validator (docx_layout.inspect_layout:
blank-page / empty-section codes) and the section start pages. With --source the 19-finding
sample is compared page by page with the source PDF (static-text similarity of section pages).
Runs with the repo venv (pypdf, lxml); no PyMuPDF needed.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import subprocess  # noqa: S404  (fixed local tool: pdftotext)
import sys
import zipfile
from pathlib import Path

from lxml import etree
from pypdf import PdfReader

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
TAG_RE = re.compile(r"\{[#/^@%]?[a-z$][\w.$]*(?:\s*[|=!&][^{}]*)?\}")
BLANK_CODES = {"DOUBLE_PAGE_BREAK", "EMPTY_SECTION", "TRAILING_EMPTY", "EMPTY_RUN"}
SECTIONS = (
    ("Inside this report", "Inside this report"),
    ("Executive overview", "Executive overview"),
    ("Assessment framework", "Assessment framework"),
    ("Coverage & tooling", "Coverage & tooling"),
    ("Remediation priorities", "Remediation priorities"),
    ("Register", "List of Vulnerable Parameter"),
    ("Disclaimer", "DISCLAIMER"),
    ("Credentials", "The expertise behind"),
    ("Thank you", "THANK YOU"),
)


def docx_text(path: Path) -> str:
    out = []
    with zipfile.ZipFile(path) as package:
        for name in package.namelist():
            if re.match(r"word/(document|header\d*|footer\d*)\.xml$", name):
                root = etree.fromstring(package.read(name))
                out += ["".join(p.itertext()) for p in root.iter(f"{W}p")]
    return "\n".join(out)


def pdf_pages(path: Path) -> list[str]:
    """Page texts via poppler's pdftotext (reading order like the PDF), pypdf as fallback."""
    if shutil.which("pdftotext"):
        out = subprocess.run(
            [
                "pdftotext",
                str(path),
                "-",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        ).stdout
        return out.split("\f")[:-1]
    return [page.extract_text() or "" for page in PdfReader(str(path)).pages]


def section_starts(pages: list[str]) -> dict[str, int]:
    starts = {}
    for name, marker in SECTIONS:
        for index, text in enumerate(pages, 1):
            if marker in text and name not in starts:
                starts[name] = index
    return starts


def layout_codes(path: Path) -> list[str]:
    try:
        from pwndoc_ai.docx_layout import inspect_layout
    except ImportError as exc:  # validator unavailable: report it rather than pass silently
        return [f"VALIDATOR_UNAVAILABLE:{exc}"]
    report = inspect_layout(path.read_bytes())
    return sorted({issue.code for issue in report.issues})


def check_sample(docx: Path, pdf: Path) -> dict:
    text = docx_text(docx)
    pages = pdf_pages(pdf)
    blank = [i for i, page in enumerate(pages, 1) if len(page.split()) < 3]
    codes = layout_codes(docx)
    return {
        "pages": len(pages),
        "leftover_tags": sorted(set(TAG_RE.findall(text)))[:10],
        "undefined": text.count("undefined"),
        "blank_pages": blank,
        "layout_codes": codes,
        "blank_codes": sorted(BLANK_CODES & set(codes)),
        "section_starts": section_starts(pages),
    }


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def compare_source(source: Path, pdf: Path) -> dict:
    """Static designed pages (cover..remediation, disclaimer..thank you) vs the source PDF."""
    src, out = pdf_pages(source), pdf_pages(pdf)
    src_starts, out_starts = section_starts(src), section_starts(out)
    sims = {}
    for name, start in src_starts.items():
        other = out_starts.get(name)
        if other is not None:
            ratio = difflib.SequenceMatcher(None, _norm(src[start - 1]), _norm(out[other - 1]))
            sims[name] = round(ratio.ratio(), 3)
    return {
        "source_pages": len(src),
        "render_pages": len(out),
        "source_starts": src_starts,
        "render_starts": out_starts,
        "similarity": sims,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("render_dir", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    result = {}
    for docx in sorted(args.render_dir.glob("*.docx")):
        pdf = docx.with_suffix(".pdf")
        if pdf.exists():
            result[docx.stem] = check_sample(docx, pdf)
    if args.source and (args.render_dir / "s19.pdf").exists():
        result["s19_vs_source"] = compare_source(args.source, args.render_dir / "s19.pdf")
    text = json.dumps(result, indent=1, ensure_ascii=False)
    if args.json:
        args.json.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
