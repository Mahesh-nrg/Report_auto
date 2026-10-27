"""Read text spans, image boxes and ruling lines of a PDF with mutool (worker side only).

``mutool draw -F stext.json`` gives spans with font name, weight, style, size and bbox, plus
image blocks. ``mutool trace`` gives the vector paths, read line by line with regular
expressions (never as XML: glyph rows may carry control characters). Both run on page ranges of
``CHUNK_PAGES`` so every output stays small, each with its own timeout and output cap.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import limits

Box = tuple[float, float, float, float]
Rule = tuple[float, float, float]  # (position, start, end)

MONO = re.compile(r"mono|courier|consolas|menlo|lucida ?console|typewriter|inconsolata", re.I)
BOLD = re.compile(r"bold|black|heavy|semibold|demi", re.I)
ITALIC = re.compile(r"italic|oblique", re.I)
XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")
PAGE_TAG = re.compile(r'<page number="\d+" mediabox="([-0-9.e ]+)"')
PATH_OPEN = re.compile(r"<(fill_path|stroke_path)\b([^>]*)>")
PATH_CLOSE = re.compile(r"</(fill_path|stroke_path)>")
POINT = re.compile(r'<(?:moveto|lineto) x="([-0-9.e]+)" y="([-0-9.e]+)"')
ATTR = re.compile(r'(\w+)="([^"]*)"')
DEFAULT_SIZE = (612.0, 792.0)


class ToolError(RuntimeError):
    """An external tool failed, timed out or wrote too much."""


@dataclass(frozen=True)
class Span:
    x0: float
    y0: float
    x1: float
    y1: float
    base: float
    size: float
    bold: bool
    italic: bool
    mono: bool
    text: str
    font: str
    block: int


@dataclass(frozen=True)
class Page:
    number: int
    width: float
    height: float
    spans: tuple[Span, ...] = ()
    images: tuple[Box, ...] = ()
    hrules: tuple[Rule, ...] = ()
    vrules: tuple[Rule, ...] = ()


def run_tool(argv: Sequence[str], timeout: float = limits.TOOL_TIMEOUT) -> bytes:
    """stdout of a tool (fixed argv, no shell); ``ToolError`` on failure, timeout or excess."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv built here, no shell
            list(argv), capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolError(type(exc).__name__) from None
    if done.returncode != 0 or len(done.stdout) > limits.MAX_TOOL_OUTPUT:
        raise ToolError("tool failed")
    return done.stdout


def _span(line: dict[str, Any], block: int) -> Span | None:
    text = XML_BAD.sub("", str(line.get("text", "")))
    font = line.get("font") or {}
    box = line.get("bbox") or {}
    if not text:
        return None
    try:
        x0, y0 = float(box["x"]), float(box["y"])
        x1, y1 = x0 + float(box["w"]), y0 + float(box["h"])
        size, base = float(font.get("size", 0)), float(line.get("y", y1))
    except (KeyError, TypeError, ValueError):
        return None
    name = str(font.get("name", "")).split("+")[-1][:100]
    return Span(
        x0, y0, x1, y1, base, size,
        bold=font.get("weight") == "bold" or bool(BOLD.search(name)),
        italic=font.get("style") == "italic" or bool(ITALIC.search(name)),
        mono=bool(MONO.search(name)),
        text=text, font=name, block=block,
    )  # fmt: skip


def _image_box(block: dict[str, Any]) -> Box | None:
    box = block.get("bbox") or {}
    try:
        x0, y0 = float(box["x"]), float(box["y"])
        return (x0, y0, x0 + float(box["w"]), y0 + float(box["h"]))
    except (KeyError, TypeError, ValueError):
        return None


def parse_stext(raw: bytes) -> list[tuple[tuple[Span, ...], tuple[Box, ...]]]:
    """Per page: the text spans and the image boxes of a ``stext.json`` document."""
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
        pages = data["pages"]
    except (ValueError, KeyError, TypeError):
        raise ToolError("bad stext") from None
    out = []
    for page in pages if isinstance(pages, list) else []:
        spans: list[Span] = []
        images: list[Box] = []
        for index, block in enumerate(page.get("blocks") or []):
            if block.get("type") == "image":
                box = _image_box(block)
                images.extend([box] if box else [])
                continue
            for line in block.get("lines") or []:
                span = _span(line, index)
                spans.extend([span] if span else [])
        out.append((tuple(spans), tuple(images)))
    return out


def _transform(attrs: str) -> tuple[float, ...]:
    values = dict(ATTR.findall(attrs))
    try:
        numbers = tuple(float(v) for v in values.get("transform", "1 0 0 1 0 0").split())
    except ValueError:
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    return numbers if len(numbers) == 6 else (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _apply(matrix: tuple[float, ...], x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return a * x + c * y + e, b * x + d * y + f


def rules_of_path(
    kind: str, points: list[tuple[float, float]], size: tuple[float, float]
) -> tuple[list[Rule], list[Rule]]:
    """Horizontal and vertical ruling segments drawn by one path (thin rects, strokes and the
    four edges of a shaded cell)."""
    if len(points) < 2:
        return [], []
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    width, height = x1 - x0, y1 - y0
    thin = 0.5 if kind == "stroke_path" else 2.0
    if height <= thin and width > 3:
        return [((y0 + y1) / 2, x0, x1)], []
    if width <= thin and height > 3:
        return [], [((x0 + x1) / 2, y0, y1)]
    big = width * height > 0.85 * size[0] * size[1]
    if kind == "fill_path" and len(points) in (4, 5) and width > 6 and height > 6 and not big:
        return [(y0, x0, x1), (y1, x0, x1)], [(x0, y0, y1), (x1, y0, y1)]
    return [], []


class _TraceReader:
    """Line-by-line state machine over ``mutool trace`` output."""

    def __init__(self) -> None:
        self.pages: list[tuple[tuple[float, float], list[Rule], list[Rule]]] = []
        self.kind = ""
        self.matrix: tuple[float, ...] = ()
        self.points: list[tuple[float, float]] = []

    def feed(self, line: str) -> None:
        if self.kind:
            self._in_path(line)
            return
        page = PAGE_TAG.search(line)
        if page:
            self.pages.append((_page_size(page.group(1)), [], []))
            return
        opened = PATH_OPEN.search(line)
        if opened and self.pages:
            self.kind, self.matrix, self.points = opened.group(1), _transform(opened.group(2)), []

    def _in_path(self, line: str) -> None:
        if PATH_CLOSE.search(line):
            size, hrules, vrules = self.pages[-1]
            if len(hrules) + len(vrules) < limits.MAX_RULES_PER_PAGE:
                more_h, more_v = rules_of_path(self.kind, self.points, size)
                hrules.extend(more_h)
                vrules.extend(more_v)
            self.kind = ""
            return
        point = POINT.search(line)
        if point and len(self.points) <= limits.MAX_TRACE_POINTS:
            try:
                x, y = float(point.group(1)), float(point.group(2))
            except ValueError:
                return
            self.points.append(_apply(self.matrix, x, y))


def _page_size(box: str) -> tuple[float, float]:
    try:
        x0, y0, x1, y1 = (float(v) for v in box.split())
    except ValueError:
        return DEFAULT_SIZE
    return (abs(x1 - x0) or DEFAULT_SIZE[0], abs(y1 - y0) or DEFAULT_SIZE[1])


def parse_trace(raw: bytes) -> list[tuple[tuple[float, float], list[Rule], list[Rule]]]:
    reader = _TraceReader()
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.lstrip().startswith("<g "):
            continue
        reader.feed(line)
    return reader.pages


def load_range(pdf: Path, first: int, last: int) -> list[Page]:
    """Pages ``first..last`` (1-based, inclusive)."""
    pages = f"{first}-{last}"
    stext = run_tool(
        ["mutool", "draw", "-q", "-F", "stext.json", "-O", "preserve-images", "-o", "-",
         str(pdf), pages]
    )  # fmt: skip
    texts = parse_stext(stext)
    try:
        geometry = parse_trace(run_tool(["mutool", "trace", str(pdf), pages]))
    except ToolError:
        geometry = []
    out = []
    for offset, (spans, images) in enumerate(texts):
        size, hrules, vrules = (
            geometry[offset] if offset < len(geometry) else (DEFAULT_SIZE, [], [])
        )
        out.append(
            Page(first + offset, size[0], size[1], spans, images, tuple(hrules), tuple(vrules))
        )
    return out


def load_pages(pdf: Path, count: int, first: int = 1) -> list[Page]:
    """Pages ``first..count`` read in chunks of ``CHUNK_PAGES``."""
    out: list[Page] = []
    for start in range(first, count + 1, limits.CHUNK_PAGES):
        out.extend(load_range(pdf, start, min(count, start + limits.CHUNK_PAGES - 1)))
    return out


__all__ = ["Box", "Page", "Rule", "Span", "ToolError", "load_pages", "parse_stext", "run_tool"]
