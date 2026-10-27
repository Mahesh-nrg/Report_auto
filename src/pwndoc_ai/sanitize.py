"""Allow-list HTML sanitiser for LLM output destined for PwnDoc rich-text fields.

PwnDoc stores finding text as HTML and renders it in the editor and in DOCX exports, so model
output (which may echo attacker-controlled web content) must never carry scripts, event
handlers or unexpected URLs. Only formatting tags survive; ``<img>`` is limited to PwnDoc image
ids (24 hex chars) or http(s) URLs.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

log = logging.getLogger(__name__)

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

# PwnDoc's HTML -> Word converter starts a paragraph only at p/div/h*/pre. Text anywhere else is
# silently dropped, a <br> outside a paragraph crashes the whole report, every "\n" becomes a <br>,
# and <img> tags are split out of the HTML by regex, so they must not sit inside other elements.
PARAGRAPH_TAGS = frozenset("p h1 h2 h3 h4 h5 h6".split())
CONTAINER_TAGS = frozenset("li blockquote th td table thead tbody tr".split())
BLOCK_TAGS = PARAGRAPH_TAGS | CONTAINER_TAGS | {"ul", "ol", "pre"}
MAX_OPEN_DEPTH = 50  # deeper start tags are dropped (their text is kept): no recursion bombs
WHITESPACE = re.compile(r"\s+")
TAG = re.compile(r"<[^>]+>")


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
        if tag not in VOID_TAGS and len(self._open) >= MAX_OPEN_DEPTH:
            return  # too deep: the tag goes, its text stays
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


@dataclass
class _Node:
    tag: str
    open: str = ""
    children: list = field(default_factory=list)  # str | _Node


class _Tree(HTMLParser):
    """Builds a tree from already-sanitised (balanced) HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("")
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in VOID_TAGS and len(self._stack) > MAX_OPEN_DEPTH:
            return  # too deep: the tag goes, its text stays
        node = _Node(tag, self.get_starttag_text() or f"<{tag}>")
        self._stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_TAGS:
            return
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)


class _Shaper:
    """Re-emits sanitised HTML in the shape PwnDoc's Word converter can handle."""

    def __init__(self) -> None:
        self._deferred: list[str] = []  # images found inside nested blocks

    def render(self, root: _Node) -> str:
        return "".join(self._blocks(root.children, top=True))

    def _take_deferred(self) -> list[str]:
        taken, self._deferred = self._deferred, []
        return taken

    def _place_image(self, tag: str, top: bool, out: list[str]) -> None:
        (out if top else self._deferred).append(tag)

    def _blocks(self, children: list, top: bool) -> list[str]:
        out: list[str] = []
        run: list = []

        def flush() -> None:
            out.extend(self._paragraphs(run, "<p>", "</p>", top))
            run.clear()

        for child in children:
            if isinstance(child, str) or (child.tag not in BLOCK_TAGS):
                run.append(child)
                continue
            flush()
            out.append(self._block(child, top))
            if top:
                out.extend(self._take_deferred())
        flush()
        return out

    def _block(self, node: _Node, top: bool) -> str:
        close = f"</{node.tag}>"
        if node.tag in PARAGRAPH_TAGS:
            if any(isinstance(c, _Node) and c.tag in BLOCK_TAGS for c in node.children):
                return "".join(self._blocks(node.children, top))  # invalid nesting: unwrap
            return "".join(self._paragraphs(node.children, node.open, close, top))
        if node.tag == "pre":
            return node.open + self._raw(node.children) + close
        return node.open + "".join(self._blocks(node.children, top=False)) + close

    def _paragraphs(self, children: list, open_tag: str, close_tag: str, top: bool) -> list[str]:
        out: list[str] = []
        buffer: list[str] = []
        images: list[str] = []

        def flush() -> None:
            text = WHITESPACE.sub(" ", "".join(buffer)).strip() if buffer else ""
            buffer.clear()
            if TAG.sub("", text).strip():
                out.append(f"{open_tag}{text}{close_tag}")
            for tag in images:
                self._place_image(tag, top, out)
            images.clear()

        for child in children:
            if isinstance(child, str):
                buffer.append(WHITESPACE.sub(" ", html.escape(child, quote=False)))
            elif child.tag == "img":
                images.append(child.open)
                flush()
            else:
                buffer.append(self._inline(child, images))
        flush()
        return out

    def _inline(self, node: _Node, images: list[str]) -> str:
        if node.tag == "br":
            return "<br>"
        if node.tag == "img":
            images.append(node.open)
            return ""
        inner = "".join(
            WHITESPACE.sub(" ", html.escape(c, quote=False))
            if isinstance(c, str)
            else self._inline(c, images)
            for c in node.children
        )
        return f"{node.open}{inner}</{node.tag}>"

    def _raw(self, children: list) -> str:
        """Preformatted text: newlines are kept (PwnDoc turns them into code-block lines)."""

        def walk(nodes: list) -> str:
            parts: list[str] = []
            for node in nodes:
                if isinstance(node, str):
                    parts.append(html.escape(node, quote=False))
                elif node.tag == "br":
                    parts.append("\n")  # a <br> inside <pre> is just a line break
                elif node.tag != "img":
                    parts.append(node.open + walk(node.children) + f"</{node.tag}>")
            return "".join(parts)

        body = walk(children)
        # Models often wrap code in blank lines; each one would become an empty Code paragraph.
        body = re.sub(r"^((?:<[^>]+>)*)\n", r"\1", body)
        return re.sub(r"\n((?:</[^>]+>)*)$", r"\1", body)


def shape_for_pwndoc(sanitised: str) -> str:
    """Give sanitised HTML the paragraph structure PwnDoc's Word export needs."""
    tree = _Tree()
    tree.feed(sanitised or "")
    tree.close()
    return _Shaper().render(tree.root)


def sanitize_html(value: str, allowed_images: frozenset[str] | None = None) -> str:
    """Sanitise ``value``. ``allowed_images`` (if given) limits <img> to those image ids."""
    parser = _Sanitizer(allowed_images)
    parser.feed(value or "")
    parser.close()
    return parser.result()


def _plain_paragraphs(text: str) -> str:
    """Escaped plain text as ``<p>`` paragraphs (blank-line separated, newlines become ``<br>``)."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return "".join(
        "<p>" + html.escape(p, quote=False).replace("\n", "<br>") + "</p>" for p in paragraphs
    )


def to_rich_text(value: str, allowed_images: frozenset[str] | None = None) -> str:
    """Turn model output into sanitised HTML; plain text becomes paragraphs."""
    text = (value or "").strip()
    if not text:
        return ""
    if not HTML_HINT.search(text):
        text = _plain_paragraphs(text)
    try:
        return shape_for_pwndoc(sanitize_html(text, allowed_images))
    except RecursionError:
        log.warning("rich text nested too deeply; kept as plain text")
        return _plain_paragraphs(value or "")


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
    return rewritten + "".join(missing)  # bare, as PwnDoc's own editor stores images
