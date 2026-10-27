"""Masking around the model call: client data out as stand-ins, real values back, fail closed.

The masker swaps client names, domains, IPs and e-mail addresses for believable stand-ins; the
helpers in :mod:`pwndoc_ai.stand_ins` decide what counts as an unrestored stand-in or a dropped
value, so every call site judges answers the same way.
"""

from __future__ import annotations

from typing import Any

from pwndoc_ai.stand_ins import dropped_original, prepare_corpus, redact_text, stray_stand_ins


class MaskAdapter:
    """Uniform use of a Masker-like object (``mask_plain``, ``leftovers``, ``prepare``,
    ``redact``, ``originals_in`` and ``dummy_leaks`` are optional)."""

    def __init__(self, masker: Any) -> None:
        self._masker = masker
        self._mask = getattr(masker, "mask_plain", None) or masker.mask

    def prepare(self, corpus: Any) -> None:
        """Register the whole document before the first chunk so no stand-in equals its text."""
        prepare_corpus(self._masker, corpus)

    def mask(self, text: str) -> str:
        """Text for the model: plain-text masking, so ``<...>`` is never taken for markup."""
        return self._mask(text)

    def unmask(self, text: str) -> str:
        restored = self._masker.unmask(text)
        return restored if isinstance(restored, str) else text

    def stray(self, text: str, original: str) -> list[str]:
        """Stand-ins in the restored ``text`` that are neither restorable nor in ``original``."""
        return stray_stand_ins(self._masker, text, original)

    def dropped(self, masked_original: str, new_text: str) -> bool:
        """True when a value that was masked in the original is missing from the new text."""
        return dropped_original(self._masker, masked_original, new_text)

    def redact(self, text: str) -> str:
        """``text`` without any client value (what may leave the machine, e.g. a web query)."""
        return redact_text(self._masker, text)
