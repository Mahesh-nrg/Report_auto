"""Shared helpers for the ParadigmIT Word template tests (build once, parse, render)."""

from __future__ import annotations

import difflib
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from lxml import etree

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts" / "paradigm"
SOURCE_PDF = REPO.parent / "report_templates" / "ParadigmIT_VAPT_Report_Reordered_Thank_You 1.pdf"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
SOFFICE_TIMEOUT = 180


def build(out: Path) -> Path:
    """Run the real generator into ``out`` (same entry point users run)."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    from build_word_template import build_document

    build_document().save(str(out))
    return out


def part(docx: Path, name: str) -> etree._Element:
    with zipfile.ZipFile(docx) as package:
        return etree.fromstring(package.read(name))


def part_names(docx: Path) -> list[str]:
    with zipfile.ZipFile(docx) as package:
        return package.namelist()


def soffice_available() -> bool:
    return shutil.which("soffice") is not None


def render_pdf(docx: Path, out_dir: Path) -> Path:
    profile = out_dir / "lo-profile"
    cmd = [
        "soffice",
        f"-env:UserInstallation=file://{profile}",
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        str(out_dir),
        str(docx),
    ]
    subprocess.run(cmd, check=True, timeout=SOFFICE_TIMEOUT, capture_output=True)
    return out_dir / (docx.stem + ".pdf")


def page_texts(pdf: Path) -> list[str]:
    """Text of every page in content-stream order (pypdf; pdftotext's column detection
    regroups the register table differently in the two PDFs)."""
    from pypdf import PdfReader

    return [page.extract_text() or "" for page in PdfReader(str(pdf)).pages]


def similarity(a: str, b: str) -> float:
    """Word-sequence similarity (order-sensitive), the metric of compare_render.py."""

    def words(text: str) -> list[str]:
        text = text.replace(" ", " ").replace("ﬁ", "fi").replace("ﬂ", "fl")
        return re.sub(r"\s+", " ", text).split()

    return difflib.SequenceMatcher(None, words(a), words(b), autojunk=False).ratio()
