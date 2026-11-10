"""Figure captions: prefix stripping and refusing ordinary text."""

from __future__ import annotations

import pytest

from pwndoc_ai.docx_import import captions, limits


@pytest.mark.parametrize(
    ("text", "caption"),
    [
        ("Fig: Burp Suite Intercept", "Burp Suite Intercept"),
        ("Figure 3: Login page", "Login page"),
        ("Figure 2 - Upload accepted", "Upload accepted"),
        ("Figure 2 \u2013 Upload accepted", "Upload accepted"),
        ("Figure 4. Response", "Response"),
        ("Fig. 5: Token", "Token"),
        ("figure 1.2: Nested number", "Nested number"),
        ("Figure 7 Request", "Request"),
        ("Screenshot 1: Admin panel", "Admin panel"),
        ("  FIG:   spaced  ", "spaced"),
        ("Fig:", ""),
    ],
)
def test_captions_lose_their_prefix(text, caption):
    assert captions.strip_caption(text) == caption


@pytest.mark.parametrize(
    "text",
    [
        "Figures show the login page",
        "Figure shows the request",
        "1. Open the page",
        "Figma: design tool",
        "Image: the attacker uploads",
        "Steps to Reproduce:",
        "",
        "Fig: first\nsecond line",
    ],
)
def test_ordinary_text_is_not_a_caption(text):
    assert captions.strip_caption(text) is None


def test_caption_is_one_line_and_capped():
    text = "Figure 1: " + "x" * (limits.MAX_CAPTION_CHARS + 50)
    assert len(captions.strip_caption(text)) == limits.MAX_CAPTION_CHARS


def test_caption_key_matches_case_and_spacing_variants():
    assert captions.caption_key("Watermark  Bypass") == captions.caption_key("watermark bypass")
