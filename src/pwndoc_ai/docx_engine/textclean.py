"""Clean-up of model output before it is compared with the original and applied as plain text."""

from __future__ import annotations

import re

# Characters XML 1.0 cannot carry (kept: TAB and NEWLINE, which become w:tab and w:br), lone
# surrogates, and the Unicode "tag" block that can hide instructions from a human reader.
_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿\U000e0000-\U000e007f]")
_BIDI = re.compile("[‪-‮⁦-⁩]")
_LINE_ENDINGS = re.compile(r"\r\n?")


def clean_model_text(text: str, original: str) -> str:
    """Plain text safe to put into a w:t: line endings unified, invalid characters removed, and
    bidirectional overrides removed unless the original paragraph already had them."""
    text = _INVALID.sub("", _LINE_ENDINGS.sub("\n", text))
    if _BIDI.search(original) is None:
        text = _BIDI.sub("", text)
    return text
