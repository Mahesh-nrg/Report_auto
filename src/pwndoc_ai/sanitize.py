"""Allow-list HTML sanitiser for LLM output destined for PwnDoc rich-text fields.

PwnDoc stores finding text as HTML and renders it in the editor and in DOCX exports, so model
output (which may echo attacker-controlled web content) must never carry scripts, event
handlers or unexpected URLs. Only formatting tags survive; ``<img>`` is limited to PwnDoc image
ids (24 hex chars) or http(s) URLs.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

ALLOWED_TAGS = frozenset(
    "p br ul ol li strong b em i u s code pre blockquote h1 h2 h3 h4 h5 h6 a img "
    "table thead tbody tr th td".split()
)
VOID_TAGS = frozenset({"br", "img"})
DROP_CONTENT = frozenset({"script", "style", "iframe", "object", "embed", "template"})
IMAGE_ID = re.compile(r"^[0-9a-f]{24}$")
SAFE_URL = re.compile(r"^(https?://|mailto:)", re.IGNORECASE)
IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
IMG_SRC = re.compile(r"""src\s*=\s*["']?([^"'\s>]+)""", re.IGNORECASE)
HTML_HINT = re.compile(r"</?(p|br|ul|ol|li|strong|em|code|pre|h[1-6]|img|a|table)\b", re.IGNORECASE)


class _Sanitizer(HTMLParser):
    def __init__(self, allowed_images: frozenset[str] | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self._allowed_images = allowed_images
        self.out: list[str] = []
        self._skip = 0
        self._open: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in DROP_CONTENT:
            self._skip += 1
            return
        if self._skip or tag not in ALLOWED_TAGS:
            return
        rendered = self._render_attrs(tag, dict(attrs))
        if rendered is None:
            return
        self.out.append(f"<{tag}{rendered}>")
        if tag not in VOID_TAGS:
            self._open.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS and tag in ALLOWED_TAGS and not self._skip:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP_CONTENT:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip or tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        if tag in self._open:
            while self._open:
                last = self._open.pop()
                self.out.append(f"</{last}>")
                if last == tag:
                    break

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.out.append(html.escape(data, quote=False))

    def _render_attrs(self, tag: str, attrs: dict[str, str | None]) -> str | None:
        if tag == "img":
            # Only PwnDoc-hosted evidence images: an external <img src> would let injected
            # model output leak finding text to a third party whenever the finding is opened.
            src = (attrs.get("src") or "").strip()
            if not IMAGE_ID.match(src):
                return None
            if self._allowed_images is not None and src not in self._allowed_images:
                return None
            alt = html.escape(attrs.get("alt") or "", quote=True)
            return f' src="{html.escape(src, quote=True)}" alt="{alt}"'
        if tag == "a":
            href = (attrs.get("href") or "").strip()
            if not SAFE_URL.match(href):
                return ""
            return f' href="{html.escape(href, quote=True)}" rel="noopener noreferrer"'
        return ""

    def result(self) -> str:
        while self._open:
            self.out.append(f"</{self._open.pop()}>")
        return "".join(self.out)


def sanitize_html(value: str, allowed_images: frozenset[str] | None = None) -> str:
    """Sanitise ``value``. ``allowed_images`` (if given) limits <img> to those image ids."""
    parser = _Sanitizer(allowed_images)
    parser.feed(value or "")
    parser.close()
    return parser.result()


def to_rich_text(value: str, allowed_images: frozenset[str] | None = None) -> str:
    """Turn model output into sanitised HTML; plain text becomes paragraphs."""
    text = (value or "").strip()
    if not text:
        return ""
    if not HTML_HINT.search(text):
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        text = "".join(
            "<p>" + html.escape(p, quote=False).replace("\n", "<br>") + "</p>" for p in paragraphs
        )
    return sanitize_html(text, allowed_images)


def image_tags(value: str) -> dict[str, str]:
    """Map image src -> full original ``<img>`` tag found in ``value``."""
    found: dict[str, str] = {}
    for tag in IMG_TAG.findall(value or ""):
        match = IMG_SRC.search(tag)
        if match:
            found.setdefault(match.group(1), tag)
    return found


def preserve_images(original: str, rewritten: str) -> str:
    """Re-append any ``<img>`` from ``original`` the model dropped from ``rewritten``.

    Evidence screenshots are the one thing that must survive a rewrite untouched.
    """
    present = image_tags(rewritten)
    # Re-render through the sanitiser so a malicious original tag is never copied verbatim.
    missing = [
        sanitize_html(tag)
        for src, tag in image_tags(original).items()
        if IMAGE_ID.match(src) and src not in present
    ]
    missing = [tag for tag in missing if tag]
    if not missing:
        return rewritten
    return rewritten + "".join(f"<p>{tag}</p>" for tag in missing)
