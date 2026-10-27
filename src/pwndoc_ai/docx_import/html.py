"""Allow-list HTML for PwnDoc rich fields (no lxml).

Two producers, one output format (contract section h):

* ``render`` turns paragraphs read from the DOCX (``Para``) into HTML: Word numbering and typed
  markers become ``ul``/``ol`` (every ``li`` holds a ``p``), soft line breaks become ``<br>``
  inside a ``p``, and a short all-bold paragraph becomes ``<p><strong>...</strong></p>``.
* ``clean_html`` re-parses untrusted HTML (UI edits, the LLM) and keeps only ``p br ul ol li
  strong em code pre``, without attributes.

Every piece of text goes through ``html.escape(quote=True)``; nothing from the input markup
(tags, attributes, link targets) reaches the output. ``<img>`` is produced only by ``image_tag``.
"""

from __future__ import annotations

import html as _html
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from html.parser import HTMLParser

from . import limits
from .labels import clean_inline

ALLOWED = frozenset({"p", "br", "ul", "ol", "li", "strong", "em", "code", "pre"})
ALIASES = {"b": "strong", "i": "em", "tt": "code", "kbd": "code", "samp": "code"}
BLOCKISH = frozenset(
    {"div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "section", "article", "header",
     "footer", "table", "tr", "td", "th", "tbody", "thead", "tfoot", "dl", "dt", "dd", "figure",
     "figcaption", "address", "main", "nav", "aside", "caption"}
)  # fmt: skip
DROP_CONTENT = frozenset(
    {"script", "style", "template", "iframe", "object", "embed", "noscript", "textarea", "title",
     "head", "svg", "math", "select", "option", "button", "form", "frame", "frameset", "noembed",
     "noframes", "xmp", "plaintext", "applet", "audio", "video", "canvas", "map", "picture"}
)  # fmt: skip
VOID = frozenset(
    {"br", "img", "hr", "input", "meta", "link", "area", "base", "col", "embed", "param",
     "source", "track", "wbr", "keygen"}
)  # fmt: skip
INLINE = frozenset({"strong", "em", "code", "br"})
MAX_DEPTH = 64
MAX_INPUT_CHARS = 4 * limits.MAX_POC_CHARS
LINE_CHARS = 20_000  # one rendered line; longer lines are cut and flagged
CHUNK_CHARS = 40_000  # a top-level list is closed and reopened past this size
SHORT_BOLD_CHARS = 200
MAX_LIST_LEVEL = 8
FIRST_MARKS = frozenset({"1", "a"})  # a typed list starting elsewhere keeps its literal numbers
MARK = re.compile(
    r"^(?:(?P<num>\d{1,3}|[a-z])[.)]|(?P<bul>[-\u2013\u2014\u2022\u00b7\u25aa\u25e6*]))\s+(?=\S)"
)
TAG = re.compile(r"<[^>]*>")
EDGE = re.compile(r"^(?:\s|<br>)+|(?:\s|<br>)+$")
SPACES = re.compile(r"\s+")


def esc(text: str) -> str:
    return _html.escape(text, quote=True)


def has_text(fragment: str) -> bool:
    return bool(TAG.sub("", fragment).strip())


# ---------------------------------------------------------------- rendering paragraphs


@dataclass(frozen=True)
class Para:
    """One DOCX paragraph as text. ``text`` uses ``\\n`` for a soft break and ``\\n\\n`` for a
    paragraph break inside it; ``list_kind`` is ``"ul"``/``"ol"`` for Word numbering, ``num_id``
    its Word list and ``number`` its ordinal at its level (0 when not counted)."""

    text: str
    list_kind: str = ""
    level: int = 0
    bold: bool = False
    num_id: str = ""
    number: int = 0


def number_lists(items: Sequence) -> list:
    """``items`` with each Word list item numbered as Word counts it: per list (``num_id``) and
    level, a deeper level restarting under each new item above it. Other items pass through."""
    counters: dict[str, list[int]] = {}
    out: list = []
    for item in items:
        if isinstance(item, Para) and item.list_kind and item.num_id and item.text.strip():
            levels = counters.setdefault(item.num_id, [0] * (MAX_LIST_LEVEL + 1))
            level = min(max(item.level, 0), MAX_LIST_LEVEL)
            levels[level] += 1
            levels[level + 1 :] = [0] * (MAX_LIST_LEVEL - level)
            item = replace(item, number=levels[level])
        out.append(item)
    return out


@dataclass(frozen=True)
class Rendered:
    chunks: tuple[str, ...]  # complete top-level elements, in order
    truncated: bool = False


@dataclass(frozen=True)
class _Line:
    text: str
    brk: str  # "p" starts a paragraph, "br" continues one
    bold: bool


def _cut(text: str) -> tuple[str, bool]:
    return (text, False) if len(text) <= LINE_CHARS else (text[:LINE_CHARS], True)


def _para_lines(para: Para) -> tuple[list[_Line], bool]:
    short_bold = para.bold and len(para.text.strip()) <= SHORT_BOLD_CHARS
    lines: list[_Line] = []
    cut = False
    for block in re.split(r"\n\s*\n", para.text):
        brk = "p"
        for raw in block.split("\n"):
            text, was_cut = _cut(clean_inline(raw))
            cut = cut or was_cut
            if text:
                lines.append(_Line(text, brk, short_bold))
                brk = "br"
    return lines, cut


def _family(text: str) -> str:
    match = MARK.match(text)
    if match is None:
        return ""
    return "ol" if match.group("num") else "ul"


def _run(lines: Sequence[_Line], start: int) -> tuple[str, list[list[str]], int]:
    """The typed list starting at ``start``: (kind, items as line lists, index after it)."""
    kind = _family(lines[start].text)
    first = MARK.match(lines[start].text)
    if not kind or (kind == "ol" and first.group("num") not in FIRST_MARKS):
        return "", [], start
    items = [[MARK.sub("", lines[start].text, count=1)]]
    index = start + 1
    while index < len(lines):
        line = lines[index]
        family = _family(line.text)
        if family == kind:
            items.append([MARK.sub("", line.text, count=1)])
        elif not family and line.brk == "br":
            items[-1].append(line.text)
        else:
            break
        index += 1
    return kind, items, index


def _list_html(kind: str, items: Sequence[Sequence[str]]) -> str:
    body = "".join(f"<li><p>{'<br>'.join(esc(t) for t in item)}</p></li>" for item in items)
    return f"<{kind}>{body}</{kind}>"


def _line_chunks(lines: Sequence[_Line]) -> list[str]:
    chunks: list[str] = []
    index = 0
    while index < len(lines):
        kind, items, after = _run(lines, index)
        if len(items) >= 2:
            chunks.append(_list_html(kind, items))
            index = after
            continue
        group = [lines[index]]
        index += 1
        while index < len(lines) and lines[index].brk == "br":
            if len(_run(lines, index)[1]) >= 2:
                break
            group.append(lines[index])
            index += 1
        inner = "<br>".join(esc(line.text) for line in group)
        if all(line.bold for line in group):
            inner = f"<strong>{inner}</strong>"
        chunks.append(f"<p>{inner}</p>")
    return chunks


class _ListStack:
    """Nested Word lists: an open ``li`` per level, closed as levels go back down."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.stack: list[tuple[str, int]] = []

    def item(self, kind: str, level: int, content: str) -> str | None:
        """Add one item; returns a finished top-level chunk when the list had to be split."""
        done = None
        if self.stack and sum(map(len, self.parts)) > CHUNK_CHARS:
            done = self.close()
        while self.stack and self.stack[-1][1] > level:
            self.parts.append(f"</li></{self.stack.pop()[0]}>")
        if self.stack and self.stack[-1][1] == level and self.stack[-1][0] != kind:
            self.parts.append(f"</li></{self.stack.pop()[0]}>")
        if self.stack and self.stack[-1][1] == level:
            self.parts.append("</li><li>")
        else:
            self.parts.append(f"<{kind}><li>")
            self.stack.append((kind, level))
        self.parts.append(content)
        return done

    def opens_new(self, level: int) -> bool:
        """True when an item at ``level`` would start a new list (none open at or above it)."""
        return not any(open_level <= level for _, open_level in self.stack)

    def close(self) -> str | None:
        while self.stack:
            self.parts.append(f"</li></{self.stack.pop()[0]}>")
        chunk = "".join(self.parts)
        self.parts = []
        return chunk or None


def _item_content(para: Para) -> tuple[str, bool]:
    lines, cut = _para_lines(para)
    paragraphs: list[list[str]] = []
    for line in lines:
        if line.brk == "p" or not paragraphs:
            paragraphs.append([])
        paragraphs[-1].append(esc(line.text))
    return "".join(f"<p>{'<br>'.join(p)}</p>" for p in paragraphs), cut


def _literal(para: Para) -> bool:
    return para.list_kind == "ol" and para.number > 1


def render(paras: Sequence[Para]) -> Rendered:
    """Allow-list HTML for ``paras``, as complete top-level chunks."""
    chunks: list[str] = []
    pending: list[_Line] = []
    lists = _ListStack()
    cut = False

    def flush() -> None:
        chunks.extend(_line_chunks(pending))
        pending.clear()

    def push(chunk: str | None) -> None:
        if chunk:
            chunks.append(chunk)

    for para in paras:
        if para.list_kind:
            flush()
            content, was_cut = _item_content(para)
            cut = cut or was_cut
            level = min(max(para.level, 0), MAX_LIST_LEVEL)
            if content and _literal(para) and lists.opens_new(level):
                push(lists.close())  # a list continued after a picture keeps its numbers
                chunks.append(f"<p>{para.number}. {content.removeprefix('<p>')}")
            elif content:
                push(lists.item(para.list_kind, level, content))
            continue
        push(lists.close())
        lines, was_cut = _para_lines(para)
        cut = cut or was_cut
        if not lines:
            flush()  # a blank paragraph ends a run of typed list items
        pending.extend(lines)
    flush()
    push(lists.close())
    return Rendered(tuple(chunks), cut)


def join_capped(chunks: Sequence[str], limit: int) -> tuple[str, bool]:
    """Whole chunks joined while the total stays within ``limit``; and whether some were cut."""
    out: list[str] = []
    total = 0
    for chunk in chunks:
        if total + len(chunk) > limit:
            return "".join(out), True
        out.append(chunk)
        total += len(chunk)
    return "".join(out), False


def group_blocks(chunks: Sequence[str], limit: int) -> list[str]:
    """Chunks grouped into PoC html blocks of at most ``limit`` characters (a single larger
    chunk is dropped; ``render`` keeps chunks well below the limit)."""
    blocks: list[str] = []
    current = ""
    for chunk in chunks:
        if len(chunk) > limit:
            continue
        if current and len(current) + len(chunk) > limit:
            blocks.append(current)
            current = ""
        current += chunk
    if current:
        blocks.append(current)
    return blocks


def image_tag(image_id: str, caption: str) -> str:
    """``<img src="{id}" alt="{escaped caption}">`` for an id PwnDoc returned."""
    if not isinstance(image_id, str) or not limits.OBJECT_ID.fullmatch(image_id):
        raise ValueError("not a PwnDoc image id")
    return f'<img src="{image_id}" alt="{esc(str(caption))}">'


# ---------------------------------------------------------------- cleaning untrusted HTML


@dataclass
class _Node:
    tag: str
    children: list


class _TreeBuilder(HTMLParser):
    """Builds a tree of allowed elements; everything else is dropped or made transparent."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", [])
        self.stack: list[_Node] = [self.root]
        self.dropping = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if tag in DROP_CONTENT and tag not in VOID:
            self.dropping += 1
            return
        if self.dropping:
            return
        if tag == "br":
            self.stack[-1].children.append(_Node("br", []))
            return
        name = "p" if tag in BLOCKISH else ALIASES.get(tag, tag)
        if name not in ALLOWED or tag in VOID or len(self.stack) > MAX_DEPTH:
            return
        if name in ("p", "li"):
            self._close_open(name)
        node = _Node(name, [])
        self.stack[-1].children.append(node)
        self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if tag == "br" and not self.dropping:
            self.stack[-1].children.append(_Node("br", []))

    def _close_open(self, name: str) -> None:
        """``<p>a<p>b`` and ``<li>a<li>b``: close the open one first (up to a list)."""
        for depth in range(len(self.stack) - 1, 0, -1):
            tag = self.stack[depth].tag
            if tag == name:
                del self.stack[depth:]
                return
            if tag in ("ul", "ol", "pre") or (name == "p" and tag == "li"):
                return

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in DROP_CONTENT and tag not in VOID:
            self.dropping = max(0, self.dropping - 1)
            return
        if self.dropping:
            return
        name = "p" if tag in BLOCKISH else ALIASES.get(tag, tag)
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth].tag == name:
                del self.stack[depth:]
                return

    def handle_data(self, data: str) -> None:
        if not self.dropping and data:
            self.stack[-1].children.append(data)


def _parse(value: str) -> _Node:
    builder = _TreeBuilder()
    builder.feed(value[:MAX_INPUT_CHARS])
    builder.close()
    return builder.root


def _inline(nodes: Sequence, active: frozenset[str] = frozenset()) -> str:
    out: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            out.append(esc(SPACES.sub(" ", node)))
        elif node.tag == "br":
            out.append("<br>")
        elif node.tag in ("strong", "em", "code"):
            inner = _inline(node.children, active | {node.tag})
            wrap = node.tag not in active and has_text(inner)
            out.append(f"<{node.tag}>{inner}</{node.tag}>" if wrap else inner)
        else:  # a block inside inline content: keep its text on its own line
            out.append(f"<br>{_inline(node.children, active)}<br>")
    return re.sub(r"(?:<br>\s*){3,}", "<br><br>", "".join(out))


def _paragraph(nodes: Sequence) -> str:
    inner = re.sub(" {2,}", " ", EDGE.sub("", _inline(nodes)))
    return f"<p>{inner}</p>" if has_text(inner) else ""


def _pre_text(node: _Node) -> str:
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif child.tag == "br":
            parts.append("\n")
        else:
            parts.append(_pre_text(child))
            if child.tag in ("p", "li"):
                parts.append("\n")
    return "".join(parts)


Block = tuple[str, str, tuple[str, ...]]  # (kind, html, li items for lists)


def _blocks(nodes: Sequence) -> list[Block]:
    blocks: list[Block] = []
    inline: list = []

    def flush() -> None:
        html = _paragraph(inline)
        if html:
            blocks.append(("p", html, ()))
        inline.clear()

    for node in nodes:
        if isinstance(node, str) or node.tag in INLINE:
            inline.append(node)
            continue
        flush()
        if node.tag == "p":
            blocks.extend(_blocks(node.children))
        elif node.tag in ("ul", "ol"):
            blocks.extend(_list_block(node.tag, node.children))
        elif node.tag == "li":
            blocks.extend(_list_block("ul", [node]))
        elif node.tag == "pre":
            text = _pre_text(node).strip("\n")
            if text.strip():
                blocks.append(("pre", f"<pre><code>{esc(text)}</code></pre>", ()))
    flush()
    return blocks


def _li_items(blocks: list[Block]) -> list[str]:
    """One ``li`` from its blocks. A li must start with a ``p``: a leading nested list is
    spliced into the parent list instead, a leading ``pre`` becomes a ``p``."""
    items: list[str] = []
    while blocks and blocks[0][0] != "p":
        kind, html, sub_items = blocks.pop(0)
        if kind == "pre":
            lines = [esc(line.strip()) for line in _html.unescape(TAG.sub("", html)).split("\n")]
            blocks.insert(0, ("p", f"<p>{'<br>'.join(t for t in lines if t)}</p>", ()))
        else:
            items.extend(sub_items)
    if blocks:
        items.append(f"<li>{''.join(b[1] for b in blocks)}</li>")
    return items


def _list_block(kind: str, children: Sequence) -> list[Block]:
    items: list[str] = []
    loose: list = []

    def flush_loose() -> None:
        if loose:
            items.extend(_li_items(_blocks(loose)))
            loose.clear()

    for child in children:
        if isinstance(child, _Node) and child.tag == "li":
            flush_loose()
            items.extend(_li_items(_blocks(child.children)))
        elif isinstance(child, _Node) and child.tag in ("ul", "ol") and items:
            nested = _list_block(child.tag, child.children)
            if nested:
                items[-1] = items[-1][: -len("</li>")] + nested[0][1] + "</li>"
        elif not (isinstance(child, str) and not child.strip()):
            loose.append(child)
    flush_loose()
    if not items:
        return []
    return [(kind, f"<{kind}>{''.join(items)}</{kind}>", tuple(items))]


def clean_html(value: str) -> str:
    """Allow-list HTML for untrusted ``value``; ``""`` when no text remains. Never raises."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        html = "".join(block[1] for block in _blocks(_parse(value).children))
    except Exception:  # noqa: BLE001 - hostile input must never break a request
        text = clean_inline(TAG.sub(" ", value[:MAX_INPUT_CHARS]))
        html = f"<p>{esc(_html.unescape(text))}</p>" if text else ""
    return html if has_text(html) else ""


def _text_lines(node: _Node, out: list[str]) -> None:
    for child in node.children:
        if isinstance(child, str):
            out.append(child)
        elif child.tag == "br":
            out.append("\n")
        else:
            block = child.tag in ("p", "li", "pre", "ul", "ol")
            if block:
                out.append("\n")
            _text_lines(child, out)
            if block:
                out.append("\n")


def plain_text(value: str) -> str:
    """Allow-list HTML as plain text: one line per paragraph, list item or line break."""
    if not isinstance(value, str) or not value.strip():
        return ""
    out: list[str] = []
    try:
        _text_lines(_parse(value), out)
    except Exception:  # noqa: BLE001 - hostile input must never break a request
        out = [_html.unescape(TAG.sub("\n", value[:MAX_INPUT_CHARS]))]
    lines = (SPACES.sub(" ", line).strip() for line in "".join(out).split("\n"))
    return "\n".join(line for line in lines if line)
