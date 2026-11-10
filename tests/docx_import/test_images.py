"""Screenshot sniffing from magic bytes, flags and the PwnDoc data URL."""

from __future__ import annotations

import base64
import random

import pytest
from reader_fixtures import GIF_1X1, gif, jpeg, png

from pwndoc_ai.docx_import import images, limits
from pwndoc_ai.docx_import.errors import ImportInputError


def test_sniff_reads_png_jpeg_and_gif_sizes():
    assert images.sniff(png(640, 480)) == ("image/png", 640, 480)
    assert images.sniff(jpeg(800, 600)) == ("image/jpeg", 800, 600)
    assert images.sniff(gif(300, 200)) == ("image/gif", 300, 200)


@pytest.mark.parametrize(
    "data",
    [
        b"RIFF\x00\x00\x00\x00WEBPVP8 ",  # webp
        b"\x01\x00\x00\x00" + b"\0" * 40 + b" EMF",  # emf
        b"\xd7\xcd\xc6\x9a" + b"\0" * 30,  # wmf
        b"<svg xmlns='http://www.w3.org/2000/svg'/>",
        b"II*\x00" + b"\0" * 20,  # tiff
        b"",
        b"\x89PNG\r\n\x1a\n",  # truncated png
        b"\xff\xd8\xff",  # truncated jpeg
        b"\xff\xd8\xff\xe0\x00\x01",  # jpeg segment length below 2
    ],
)
def test_unsupported_or_broken_bytes_have_no_type(data):
    assert images.sniff(data)[0] == ""


def test_sniff_never_raises_on_random_bytes():
    rng = random.Random(7)
    for _ in range(500):
        prefix = rng.choice([b"", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF89a"])
        data = prefix + bytes(rng.randrange(256) for _ in range(rng.randrange(60)))
        media_type, width, height = images.sniff(data)
        assert media_type in ("", *limits.IMAGE_TYPES) and width >= 0 and height >= 0


def test_data_url_uses_the_sniffed_type():
    data = jpeg()
    url = images.data_url(data)
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == data


def test_data_url_refuses_unknown_and_oversized(monkeypatch):
    with pytest.raises(ImportInputError) as caught:
        images.data_url(b"not an image at all")
    assert caught.value.code == "image_not_usable"
    monkeypatch.setattr(limits, "MAX_IMAGE_BYTES", 100)
    with pytest.raises(ImportInputError):
        images.data_url(png(pad=500))


def test_facts_flag_placeholders():
    assert "placeholder" in images.facts_for(GIF_1X1, size=len(GIF_1X1)).flags
    small = png(10, 10, pad=0)
    assert "placeholder" in images.facts_for(small, size=len(small)).flags
    one = png(1, 1)
    assert "placeholder" in images.facts_for(one, size=len(one)).flags
    real = png()
    facts = images.facts_for(real, size=len(real))
    assert facts.flags == () and facts.usable


def test_facts_flag_unsupported_and_too_large(monkeypatch):
    other = b"RIFF" + b"\0" * 400
    facts = images.facts_for(other, size=len(other))
    assert "unsupported" in facts.flags and not facts.usable
    huge = png(limits.MAX_IMAGE_PIXELS + 1, 10)
    facts = images.facts_for(huge, size=len(huge))
    assert "too_large" in facts.flags and (facts.width, facts.height) == (0, 0)
    monkeypatch.setattr(limits, "MAX_IMAGE_BYTES", 100)
    facts = images.facts_for(png()[:50], size=5000, digest="a" * 64)
    assert facts.flags == ("too_large",) and facts.sha256 == "a" * 64 and not facts.usable


def test_extension_follows_the_type():
    assert images.extension("image/jpeg") == "jpg"
    assert images.extension("") == "bin"
