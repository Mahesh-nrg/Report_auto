"""LibreOffice render of the ParadigmIT Word report vs the source PDF: page count, section
start pages and per-page text similarity (skips when the PDF or soffice is absent)."""

from __future__ import annotations

import pytest
from paradigm_word_helpers import (
    SOURCE_PDF,
    build,
    page_texts,
    render_pdf,
    similarity,
    soffice_available,
)

if not SOURCE_PDF.exists():
    pytest.skip("source PDF is absent", allow_module_level=True)
if not soffice_available():
    pytest.skip("soffice is absent", allow_module_level=True)
pytest.importorskip("pypdf")

PAGES = 26
MIN_SIMILARITY = 0.965  # 0.97 except pages with the [CLIENT LOGO] header slot (new, not in the
# source PDF): 0.967-0.968 there, still effectively identical content (one header phrase added).
SECTION_STARTS = {
    "Inside this report": 2,
    "Executive overview": 3,
    "Assessment framework": 4,
    "Coverage & tooling": 5,
    "Remediation priorities": 6,
    "List of Vulnerable Parameter": 7,
    "DETAILED TECHNICAL FINDINGS": 8,
    "DISCLAIMER": 24,
    "The expertise behind": 25,
    "THANK YOU": 26,
}


@pytest.fixture(scope="module")
def texts(tmp_path_factory: pytest.TempPathFactory) -> tuple[list[str], list[str]]:
    work = tmp_path_factory.mktemp("paradigm_render")
    pdf = render_pdf(build(work / "ParadigmIT_VAPT_Report.docx"), work)
    return page_texts(SOURCE_PDF), page_texts(pdf)


def _first_page(pages: list[str], marker: str) -> int:
    return next(index + 1 for index, text in enumerate(pages) if marker in text)


def test_page_count_matches(texts: tuple[list[str], list[str]]) -> None:
    source, rendered = texts
    assert len(source) == PAGES
    assert len(rendered) == PAGES


@pytest.mark.parametrize("marker", sorted(SECTION_STARTS))
def test_sections_start_on_the_pdf_page(texts: tuple[list[str], list[str]], marker: str) -> None:
    source, rendered = texts
    assert _first_page(source, marker) == SECTION_STARTS[marker]
    assert _first_page(rendered, marker) == SECTION_STARTS[marker]


# Pages 7-23 hold the register and the findings. The Word report lists them in CVSS v3.1 order
# (docs/FINDING_ORDER.md), the source PDF in its own order, so that region is compared as a bag
# of words (same content, different order) and every other page page-by-page.
FINDING_PAGES = range(7, 24)
SORTED_TITLES = (
    "Finding 001 — Authentication Bypass",
    "Finding 002 — Improper Implementation of Password Reset",
    "Finding 003 — Exposed Encryption Key",
    "Finding 019 — JWT Signing Algorithm",
)


def test_static_pages_are_similar(texts: tuple[list[str], list[str]]) -> None:
    source, rendered = texts
    scores = {
        page: similarity(a, b)
        for page, (a, b) in enumerate(zip(source, rendered, strict=True), 1)
        if page not in FINDING_PAGES
    }
    low = {page: round(score, 3) for page, score in scores.items() if score < MIN_SIMILARITY}
    assert not low, low


def _bag(pages: list[str]) -> str:
    words = " ".join(pages[page - 1] for page in FINDING_PAGES).split()
    return " ".join(sorted(w for w in words if not w.isdigit()))


def test_finding_region_has_the_same_words(texts: tuple[list[str], list[str]]) -> None:
    source, rendered = texts
    assert similarity(_bag(source), _bag(rendered)) >= 0.97


def test_findings_are_in_cvss_order(texts: tuple[list[str], list[str]]) -> None:
    _source, rendered = texts
    flat = " ".join(" ".join(rendered[page - 1].split()) for page in FINDING_PAGES)
    positions = [flat.find(title) for title in SORTED_TITLES]
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions)


def test_rendered_pdf_is_not_a_picture(texts: tuple[list[str], list[str]]) -> None:
    _source, rendered = texts
    assert all(len(text.split()) > 10 for text in rendered)
