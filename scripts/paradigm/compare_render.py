# ruff: noqa: S603, S607  (fixed local tools: soffice, pdftoppm, montage)
"""Render a DOCX with LibreOffice and compare it page by page with the source PDF.

    PYTHON compare_render.py SOURCE.pdf REPORT.docx --out DIR [--md REPORT.md] [--previews DIR]

Needs PyMuPDF (``pymupdf``) for span analysis: run it with an analysis venv, not the repo venv.
Reports per page: text similarity, font / size / colour differences of matched text spans
(grouped by role = font + size of the source span), baseline offsets, images (count and pixel
aspect) and writes side-by-side montages (source left, Word render right) at 60 dpi.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

import pymupdf

SOFFICE_TIMEOUT = 180
SECTION_MARKERS = (
    ("Cover", "Vulnerability"),
    ("Inside this report", "Inside this report"),
    ("Executive overview", "Executive overview"),
    ("Assessment framework", "Assessment framework"),
    ("Coverage & tooling", "Coverage & tooling"),
    ("Remediation priorities", "Remediation priorities"),
    ("5. Register", "List of Vulnerable Parameter"),
    ("06 Detailed findings", "DETAILED TECHNICAL"),
    ("07 Disclaimer", "DISCLAIMER"),
    ("08 Credentials", "The expertise behind"),
    ("Thank you", "THANK YOU"),
)


@dataclass
class Span:
    text: str
    font: str
    size: float
    color: str
    x: float
    y: float


@dataclass
class PageResult:
    page: int
    similarity: float
    font_diffs: list[str] = field(default_factory=list)
    dy_median: float = 0.0
    dy_max: float = 0.0
    dx_median: float = 0.0
    images_src: list[str] = field(default_factory=list)
    images_out: list[str] = field(default_factory=list)
    unmatched: int = 0


def render_docx(docx: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.gettempdir()) / "paradigm" / "lo" / "compare"
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
    subprocess.run(cmd, check=True, timeout=SOFFICE_TIMEOUT, capture_output=True)  # noqa: S603
    return out_dir / (docx.stem + ".pdf")


def _font_name(raw: str) -> str:
    return raw.split("+", 1)[-1]


def page_spans(page: pymupdf.Page) -> list[Span]:
    spans = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                text = span["text"].strip()
                if not text:
                    continue
                spans.append(
                    Span(
                        text,
                        _font_name(span["font"]),
                        round(span["size"], 1),
                        f"#{span['color']:06x}",
                        round(span["origin"][0], 1),
                        round(span["origin"][1], 1),
                    )
                )
    return spans


def _norm(text: str) -> str:
    text = text.replace(" ", " ").replace("ﬁ", "fi").replace("ﬂ", "fl")
    return re.sub(r"\s+", " ", text).strip()


def text_similarity(a: str, b: str) -> float:
    wa, wb = _norm(a).split(), _norm(b).split()
    return round(difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio(), 4)


def _words(spans: list[Span]) -> list[tuple[str, Span]]:
    out = []
    for span in spans:
        for word in _norm(span.text).split():
            out.append((word, span))
    return out


def _match_words(src: list[Span], out: list[Span]) -> list[tuple[Span, Span]]:
    a, b = _words(src), _words(out)
    matcher = difflib.SequenceMatcher(None, [w for w, _ in a], [w for w, _ in b], autojunk=False)
    pairs: list[tuple[Span, Span]] = []
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            pairs.append((a[block.a + k][1], b[block.b + k][1]))
    return pairs


def _family(font: str) -> str:
    return font.replace("MT", "").replace(",", "-")


def compare_fonts(pairs: list[tuple[Span, Span]]) -> list[str]:
    seen: Counter[str] = Counter()
    for src, out in pairs:
        role = f"{src.font} {src.size}"
        problems = []
        if _family(src.font) != _family(out.font):
            problems.append(f"font {out.font}")
        if abs(src.size - out.size) > 0.15:
            problems.append(f"size {out.size}")
        if src.color.lower() != out.color.lower():
            problems.append(f"colour {out.color} (src {src.color})")
        if problems:
            seen[f"{role}: " + ", ".join(problems)] += 1
    return [f"{key} x{count}" for key, count in seen.most_common()]


def _offsets(pairs: list[tuple[Span, Span]]) -> tuple[float, float, float]:
    dys = [out.y - src.y for src, out in pairs]
    dxs = [out.x - src.x for src, out in pairs]
    if not dys:
        return 0.0, 0.0, 0.0
    return (round(median(dys), 1), round(max(dys, key=abs), 1), round(median(dxs), 1))


def page_images(page: pymupdf.Page) -> list[str]:
    out = []
    for info in page.get_image_info(xrefs=True):
        x0, y0, x1, y1 = info["bbox"]
        width, height = info.get("width") or 0, info.get("height") or 0
        aspect = round(width / height, 3) if height else 0
        out.append(f"{width}x{height}@{x1 - x0:.0f}x{y1 - y0:.0f}pt a={aspect}")
    return sorted(out)


def compare_page(index: int, src: pymupdf.Page, out: pymupdf.Page | None) -> PageResult:
    if out is None:
        return PageResult(index + 1, 0.0, ["page missing in render"])
    pairs = _match_words(page_spans(src), page_spans(out))
    dy_med, dy_max, dx_med = _offsets(pairs)
    result = PageResult(
        index + 1,
        text_similarity(src.get_text(), out.get_text()),
        compare_fonts(pairs),
        dy_med,
        dy_max,
        dx_med,
        page_images(src),
        page_images(out),
    )
    result.unmatched = len(_words(page_spans(src))) - len(pairs)
    return result


def section_pages(doc: pymupdf.Document) -> dict[str, int]:
    found: dict[str, int] = {}
    for name, marker in SECTION_MARKERS:
        for number, page in enumerate(doc, start=1):
            if marker in page.get_text():
                found[name] = number
                break
    return found


def montage(src_pdf: Path, out_pdf: Path, previews: Path, dpi: int = 60) -> list[Path]:
    """Side-by-side PNG per page (source | render) plus contact sheets of 6 pages."""
    previews.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="paradigm_montage_"))
    try:
        for tag, pdf in (("src", src_pdf), ("out", out_pdf)):
            subprocess.run(
                ["pdftoppm", "-r", str(dpi), "-png", str(pdf), str(tmp / tag)],  # noqa: S603,S607
                check=True,
                timeout=SOFFICE_TIMEOUT,
            )
        pairs = []
        for src_png in sorted(tmp.glob("src-*.png")):
            number = src_png.stem.split("-")[-1]
            out_png = tmp / f"out-{number}.png"
            target = previews / f"page-{int(number):02d}.png"
            files = [str(src_png)] + ([str(out_png)] if out_png.exists() else [])
            subprocess.run(
                [
                    "montage",
                    *files,
                    "-tile",
                    "2x1",
                    "-geometry",
                    "+6+0",  # noqa: S603,S607
                    "-background",
                    "#888888",
                    str(target),
                ],
                check=True,
                timeout=120,
            )
            pairs.append(target)
        return _sheets(pairs, previews)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _sheets(pairs: list[Path], previews: Path, per_sheet: int = 6) -> list[Path]:
    sheets = []
    for start in range(0, len(pairs), per_sheet):
        chunk = pairs[start : start + per_sheet]
        target = previews / f"sheet-{start // per_sheet + 1}.png"
        subprocess.run(
            [
                "montage",
                *map(str, chunk),
                "-tile",
                "3x",
                "-geometry",
                "+8+8",  # noqa: S603,S607
                "-background",
                "#444444",
                str(target),
            ],
            check=True,
            timeout=120,
        )
        sheets.append(target)
    return sheets


def compare(src_pdf: Path, out_pdf: Path) -> dict:
    src, out = pymupdf.open(src_pdf), pymupdf.open(out_pdf)
    pages = [
        compare_page(i, src[i], out[i] if i < out.page_count else None)
        for i in range(src.page_count)
    ]
    return {
        "src_pages": src.page_count,
        "out_pages": out.page_count,
        "sections_src": section_pages(src),
        "sections_out": section_pages(out),
        "pages": pages,
    }


def _page_row(page: PageResult) -> str:
    images = (
        "same"
        if page.images_src == page.images_out
        else (f"src {len(page.images_src)} / out {len(page.images_out)}")
    )
    fonts = "; ".join(page.font_diffs[:4]) or "none"
    return (
        f"| {page.page} | {page.similarity:.3f} | {page.dy_median:+.1f} / {page.dy_max:+.1f} "
        f"| {page.dx_median:+.1f} | {images} | {fonts} |"
    )


def markdown(result: dict, notes: dict[str, str] | None = None) -> str:
    pages: list[PageResult] = result["pages"]
    sims = [p.similarity for p in pages]
    lines = [
        "# Word render vs source PDF",
        "",
        f"- Pages: source {result['src_pages']}, Word (LibreOffice render) {result['out_pages']}",
        f"- Text similarity: mean {sum(sims) / len(sims):.4f}, min {min(sims):.4f} "
        f"(page {pages[sims.index(min(sims))].page})",
        "",
        "## Section start pages",
        "",
        "| Section | PDF | Word |",
        "|---|---|---|",
    ]
    for name, page in result["sections_src"].items():
        lines.append(f"| {name} | {page} | {result['sections_out'].get(name, '-')} |")
    lines += [
        "",
        "## Per page",
        "",
        "dy/dx = baseline offset of matched words (render - PDF, pt): median / worst. The worst "
        'value includes stray matches of repeated tokens ("/", "N/A"), so read it with the median.',
        "",
        "| Page | Text sim. | dy med / max | dx med | Images | Font / size / colour diffs |",
        "|---|---|---|---|---|---|",
    ]
    lines += [_page_row(page) for page in pages]
    if notes:
        lines += ["", "## Visual notes", ""]
        ordered = sorted(notes.items(), key=lambda item: int(item[0].split("-")[0]))
        lines += [f"- p{page}: {note}" for page, note in ordered]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("docx", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--md", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--previews", type=Path)
    parser.add_argument("--notes", type=Path, help="JSON {page: note} merged into the report")
    parser.add_argument("--appendix", type=Path, help="Markdown appended to the report")
    args = parser.parse_args()
    rendered = render_docx(args.docx, args.out)
    result = compare(args.source, rendered)
    notes = None
    if args.notes and args.notes.exists():
        notes = dict(json.loads(args.notes.read_text()))
    report = markdown(result, notes)
    if args.appendix and args.appendix.exists():
        report += "\n" + args.appendix.read_text(encoding="utf-8")
    if args.md:
        args.md.write_text(report, encoding="utf-8")
    if args.json:
        payload = {**result, "pages": [page.__dict__ for page in result["pages"]]}
        args.json.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    if args.previews:
        montage(args.source, rendered, args.previews)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


_ = defaultdict  # kept for callers extending the report
