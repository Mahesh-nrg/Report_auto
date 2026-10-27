"""Figure captions: ``"Fig: Burp request"``, ``"Figure 3: Login"``, ``"Figure 2 - Upload"``.

A caption names the screenshot directly before it (the template writes ``{%image}`` and then
``Fig: {caption}``). ``strip_caption`` returns the caption without its prefix, or ``None`` when
the paragraph is ordinary text, so "Figures show..." or "1. Open the page" are never captions.
"""

from __future__ import annotations

import re

from . import limits
from .labels import cap, clean_line, norm

CAPTION_RE = re.compile(
    r"""^\s*
    (?:fig(?:ure)?|screenshot|image)\.?          # the word
    (?:\s*(?P<num>\d{1,4}(?:[.-]\d{1,4})?)\.?)?   # an optional number, e.g. 3 or 2.1
    \s*(?P<sep>[:\-.|)])?                         # a separator
    \s*(?P<rest>.*)$""",
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)


def strip_caption(text: str) -> str | None:
    """The caption text without its ``Fig``/``Figure N:``/``Figure N -``/``Figure N.`` prefix;
    ``None`` when ``text`` is not a caption. A caption needs a separator or a number after the
    word (``"Figure shows"`` is prose) and is a single line."""
    value = norm(text)
    if not value or "\n" in str(text).strip():
        return None
    match = CAPTION_RE.match(value)
    if match is None or (match.group("num") is None and match.group("sep") is None):
        return None
    if match.group("num") is None and not re.match(r"(?i)fig", value):
        return None  # "Image: x" and "Screenshot: x" need a number to count as captions
    rest = clean_line(match.group("rest"))
    caption, _ = cap(rest, limits.MAX_CAPTION_CHARS)
    return caption


def caption_key(caption: str) -> str:
    """Key used to spot a repeated figure (same picture, same caption)."""
    return norm(caption).casefold()
