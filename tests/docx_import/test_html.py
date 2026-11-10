"""Allow-list HTML: rendering DOCX paragraphs and cleaning untrusted HTML."""

from __future__ import annotations

import random
import re

import pytest

from pwndoc_ai.docx_import import html as h
from pwndoc_ai.docx_import.html import Para, number_lists

ALLOWED_TAG = re.compile(r"</?(p|br|ul|ol|li|strong|em|code|pre)>")


def only_allowed(fragment: str) -> bool:
    return all(ALLOWED_TAG.fullmatch(tag) for tag in re.findall(r"<[^>]*>", fragment))


def lis_have_p(fragment: str) -> bool:
    return all(fragment.startswith("<p>", m.end()) for m in re.finditer(r"<li>", fragment))


def joined(paras) -> str:
    return "".join(h.render(paras).chunks)


# ---------------------------------------------------------------- render


def test_paragraphs_and_soft_breaks():
    out = joined([Para("first line\nsecond line"), Para("next\n\nparagraph")])
    assert out == "<p>first line<br>second line</p><p>next</p><p>paragraph</p>"


def test_word_numbering_becomes_nested_lists():
    out = joined(
        [
            Para("one", "ol", 0),
            Para("two", "ol", 0),
            Para("bullet below two", "ul", 1),
            Para("three", "ol", 0),
            Para("after"),
        ]
    )
    assert out == (
        "<ol><li><p>one</p></li><li><p>two</p><ul><li><p>bullet below two</p></li></ul></li>"
        "<li><p>three</p></li></ol><p>after</p>"
    )


def test_list_kind_change_on_one_level_starts_a_new_list():
    out = joined([Para("a", "ul"), Para("b", "ol")])
    assert out == "<ul><li><p>a</p></li></ul><ol><li><p>b</p></li></ol>"


def test_list_item_with_soft_break_keeps_it_inside_the_paragraph():
    out = joined([Para("Construct the URL:\nhttps://a/thumbnail", "ol")])
    assert out == "<ol><li><p>Construct the URL:<br>https://a/thumbnail</p></li></ol>"


def test_typed_markers_need_two_items():
    assert joined([Para("1. only one")]) == "<p>1. only one</p>"
    assert joined([Para("1. first"), Para("2. second")]) == (
        "<ol><li><p>first</p></li><li><p>second</p></li></ol>"
    )
    assert joined([Para("- a\n- b")]) == "<ul><li><p>a</p></li><li><p>b</p></li></ul>"
    assert (
        joined([Para("\u2022 a"), Para("\u2022 b")])
        == "<ul><li><p>a</p></li><li><p>b</p></li></ul>"
    )


def test_typed_list_inside_one_paragraph_after_a_heading_line():
    out = joined([Para("Steps to Reproduce:\n\n1. Identify\n2. Locate\ncontinued")])
    assert out == (
        "<p>Steps to Reproduce:</p><ol><li><p>Identify</p></li><li><p>Locate<br>continued</p></li></ol>"
    )


def test_blank_paragraph_breaks_a_typed_run():
    out = joined([Para("1. a"), Para(""), Para("2. b")])
    assert out == "<p>1. a</p><p>2. b</p>"


def test_short_all_bold_paragraph_is_strong():
    assert joined([Para("Impact:", bold=True)]) == "<p><strong>Impact:</strong></p>"
    long_text = "x" * (h.SHORT_BOLD_CHARS + 1)
    assert joined([Para(long_text, bold=True)]) == f"<p>{long_text}</p>"


def test_text_is_escaped():
    out = joined([Para("GET /assets/<uuid>?a=1&b=\"2\" 'x'")])
    assert out == "<p>GET /assets/&lt;uuid&gt;?a=1&amp;b=&quot;2&quot; &#x27;x&#x27;</p>"
    assert "<uuid>" not in out


def test_blank_paragraphs_and_empty_list_items_emit_nothing():
    assert joined([Para(""), Para("  \n "), Para("", "ul")]) == ""


def test_render_output_is_already_clean():
    paras = [
        Para("Title", bold=True),
        Para("a <b> & c\nnext"),
        Para("x", "ul"),
        Para("y", "ul", 2),
        Para("z", "ol"),
        Para("1. p\n2. q"),
    ]
    out = joined(paras)
    assert h.clean_html(out) == out
    assert only_allowed(out) and lis_have_p(out)


def test_long_lines_are_cut_and_flagged():
    rendered = h.render([Para("x" * (h.LINE_CHARS + 10))])
    assert rendered.truncated and len(rendered.chunks[0]) == h.LINE_CHARS + len("<p></p>")


def test_huge_lists_are_split_into_chunks_below_the_limit():
    paras = [Para(f"item {i} " + "y" * 200, "ul") for i in range(600)]
    chunks = h.render(paras).chunks
    assert len(chunks) > 1
    assert all(len(c) < h.CHUNK_CHARS + 1000 for c in chunks)
    assert all(h.clean_html(c) == c for c in chunks)


def test_join_capped_and_group_blocks():
    assert h.join_capped(["<p>a</p>", "<p>b</p>"], 9) == ("<p>a</p>", True)
    assert h.join_capped(["<p>a</p>"], 100) == ("<p>a</p>", False)
    assert h.group_blocks(["<p>a</p>", "<p>b</p>", "<p>c</p>"], 16) == [
        "<p>a</p><p>b</p>",
        "<p>c</p>",
    ]


# ---------------------------------------------------------------- image_tag


def test_image_tag_escapes_the_caption():
    tag = h.image_tag("a" * 24, 'Login "admin" <b> & co')
    assert tag == '<img src="' + "a" * 24 + '" alt="Login &quot;admin&quot; &lt;b&gt; &amp; co">'


@pytest.mark.parametrize("bad", ["", "a" * 23, "A" * 24, "javascript:alert(1)", "a" * 24 + '"'])
def test_image_tag_refuses_anything_but_an_object_id(bad):
    with pytest.raises(ValueError):
        h.image_tag(bad, "x")


# ---------------------------------------------------------------- clean_html


@pytest.mark.parametrize(
    ("dirty", "clean"),
    [
        ("<script>alert(1)</script><p>ok</p>", "<p>ok</p>"),
        ('<p onclick="x()" style="color:red" class="c">hi</p>', "<p>hi</p>"),
        ('<a href="javascript:alert(1)">link</a>', "<p>link</p>"),
        ('<p>a<img src=x onerror="alert(1)">b</p>', "<p>ab</p>"),
        ("<p><img src=x><img src=y>t</p>", "<p>t</p>"),
        ("<style>p{}</style><svg><script>x</script><text>s</text></svg>t", "<p>t</p>"),
        ("&lt;script&gt;alert(1)&lt;/script&gt;", "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"),
        ("&amp;&quot;&#39;&#x3c;", "<p>&amp;&quot;&#x27;&lt;</p>"),
        ("<b>bold</b> <i>it</i>", "<p><strong>bold</strong> <em>it</em></p>"),
        ("<ul><li>a</li><li>b</li></ul>", "<ul><li><p>a</p></li><li><p>b</p></li></ul>"),
        ("<ul><li>a<li>b</ul>", "<ul><li><p>a</p></li><li><p>b</p></li></ul>"),
        ("<li>loose</li>", "<ul><li><p>loose</p></li></ul>"),
        ("text<br>more", "<p>text<br>more</p>"),
        ("<br><br><p><br>x<br></p>", "<p>x</p>"),
        ("<p></p><strong></strong><code> </code><p> </p>", ""),
        ("<div>one</div><div>two</div>", "<p>one</p><p>two</p>"),
        ("<h1>Head</h1>body", "<p>Head</p><p>body</p>"),
        ("<pre>line1\n  line2 <b>x</b></pre>", "<pre><code>line1\n  line2 x</code></pre>"),
        ("<!-- c --><![CDATA[x]]><?pi?><p>k</p>", "<p>k</p>"),
        ("<p>unclosed <strong>bold", "<p>unclosed <strong>bold</strong></p>"),
        ("<strong><strong>x</strong></strong>", "<p><strong>x</strong></p>"),
        ("<ul><li><ul><li>deep</li></ul></li></ul>", "<ul><li><p>deep</p></li></ul>"),
        (
            "<ul><li>a</li><ul><li>b</li></ul></ul>",
            "<ul><li><p>a</p><ul><li><p>b</p></li></ul></li></ul>",
        ),
        ("<ol><li><pre>code</pre></li></ol>", "<ol><li><p>code</p></li></ol>"),
        ("<mark>m</mark><iframe src=x>i</iframe>", "<p>m</p>"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_clean_html_allow_list(dirty, clean):
    assert h.clean_html(dirty) == clean


def test_clean_html_ignores_non_strings():
    assert h.clean_html(None) == ""  # type: ignore[arg-type]
    assert h.plain_text(123) == ""  # type: ignore[arg-type]


def test_clean_html_survives_deep_nesting_and_huge_input():
    deep = "<ul><li>" * 500 + "x" + "</li></ul>" * 500
    out = h.clean_html(deep)
    assert "x" in out and only_allowed(out) and lis_have_p(out)
    big = "<p>" + "a" * (h.MAX_INPUT_CHARS * 2) + "</p>"
    assert len(h.clean_html(big)) <= h.MAX_INPUT_CHARS + 20


def test_clean_html_fuzz_never_raises_and_only_emits_the_allow_list():
    rng = random.Random(11)
    pieces = ["<p>", "</p>", "<ul>", "</ul>", "<ol>", "<li>", "</li>", "<br>", "<b>", "</b>",
              "<script>", "</script>", "<img src=x>", "<div>", "</div>", "<pre>", "</pre>",
              "<code>", "</code>", "&amp;", "&#0;", "&#x110000;", "<", ">", '"', "text", " ",
              "<a href='x'>", "</a>", "<!--", "-->", "<svg>", "</svg>", "\n", "<em>", "</em>"]  # fmt: skip
    for _ in range(400):
        value = "".join(rng.choice(pieces) for _ in range(rng.randrange(1, 40)))
        out = h.clean_html(value)
        assert only_allowed(out), (value, out)
        assert lis_have_p(out), (value, out)
        assert "<p></p>" not in out and "<code></code>" not in out
        assert h.clean_html(out) == out, (value, out)


def test_br_only_appears_inside_paragraphs():
    out = h.clean_html("<ul><li>a<br>b</li></ul><br>c<pre>x<br>y</pre>")
    for match in re.finditer("<br>", out):
        before = out[: match.start()]
        assert before.rfind("<p>") > before.rfind("</p>")


# ---------------------------------------------------------------- plain_text


def test_plain_text_lines():
    value = "<p>a<br>b</p><ul><li><p>c</p></li></ul><pre><code>d</code></pre><p>&amp; &lt;x&gt;</p>"
    assert h.plain_text(value) == "a\nb\nc\nd\n& <x>"


def test_plain_text_of_hostile_input_drops_scripts():
    assert h.plain_text("<script>secret()</script><p>shown</p>") == "shown"
    assert h.plain_text("") == ""


# -- closer review (COR-7): numbered steps keep their numbers ------------------------------------
def test_a_typed_list_starting_after_1_keeps_its_literal_numbers():
    out = joined([Para("5. Observe the response"), Para("6. Locate the file")])
    assert out == "<p>5. Observe the response</p><p>6. Locate the file</p>"
    assert joined([Para("1. a"), Para("2. b")]) == "<ol><li><p>a</p></li><li><p>b</p></li></ol>"


def test_word_list_items_are_counted_per_list_and_level():
    items = number_lists([
        Para("s1", "ol", 0, num_id="3"), Para("sub", "ol", 1, num_id="3"),
        Para("s2", "ol", 0, num_id="3"), Para("sub2", "ol", 1, num_id="3"),
        Para("other", "ol", 0, num_id="4"), Para("plain"),
    ])  # fmt: skip
    assert [getattr(i, "number", 0) for i in items] == [1, 1, 2, 1, 1, 0]


def test_a_word_list_cut_by_a_picture_continues_with_its_numbers():
    steps = number_lists([Para(f"step {n}", "ol", 0, num_id="7") for n in range(1, 7)])
    assert joined(steps[:4]).startswith("<ol><li><p>step 1</p>")
    assert joined(steps[4:]) == "<p>5. step 5</p><p>6. step 6</p>"


def test_a_nested_list_under_a_continued_step_still_nests():
    steps = number_lists(
        [Para("a", "ol", 0, num_id="7"), Para("b", "ol", 0, num_id="7"),
         Para("detail", "ul", 1, num_id="7"), Para("c", "ol", 0, num_id="7")]
    )  # fmt: skip
    assert joined(steps[1:]) == "<p>2. b</p><ul><li><p>detail</p></li></ul><p>3. c</p>"
