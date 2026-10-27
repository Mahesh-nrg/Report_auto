"""Normalisers of the typography settings (default, heading and mono font and size).

Contract: docs/EXPORT_DESIGN.md (d). A family goes through ``font_catalog_model.clean_family``
(no fontconfig or CSS syntax, no leading ``-``, so it can never become a command-line option);
``""`` switches the setting off. A size is clamped leniently to 6-72 pt in 0.5 steps, ``0`` is
off. Errors are ``ValueError`` subclasses raised by the caller-supplied factory so config.py can
keep its own ``SettingsError`` type; every message names the key.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .export_errors import ExportInputError
from .font_catalog_model import clamp_size, clean_family

FONT_KEYS = ("default_font", "heading_font", "mono_font")
SIZE_KEYS = ("default_font_size", "heading_font_size", "mono_font_size")
FONT_ENV = {
    "PWNDOC_AI_DEFAULT_FONT": "default_font",
    "PWNDOC_AI_DEFAULT_FONT_SIZE": "default_font_size",
    "PWNDOC_AI_HEADING_FONT": "heading_font",
    "PWNDOC_AI_HEADING_FONT_SIZE": "heading_font_size",
    "PWNDOC_AI_MONO_FONT": "mono_font",
    "PWNDOC_AI_MONO_FONT_SIZE": "mono_font_size",
}

ErrorFactory = Callable[[str], ValueError]


def font_name(key: str, error: ErrorFactory) -> Callable[[Any], str]:
    """The normaliser of a family setting ``key``: ``None``/blank is off, else a clean name."""

    def normalise(value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise error(f"{key} is not a valid font name")
        try:
            return clean_family(value.strip(), key)
        except ExportInputError as exc:
            raise error(f"{key} is not a valid font name") from exc

    return normalise


def font_size(key: str, error: ErrorFactory) -> Callable[[Any], float]:
    """The normaliser of a size setting ``key``: 0 is off, else clamped to 6-72 in 0.5 steps."""

    def normalise(value: Any) -> float:
        try:
            return clamp_size(value)
        except ValueError as exc:
            raise error(f"{key} must be a number") from exc

    return normalise


def normalisers(error: ErrorFactory) -> dict[str, Callable[[Any], Any]]:
    """Every typography setting key -> its normaliser."""
    names = {key: font_name(key, error) for key in FONT_KEYS}
    sizes = {key: font_size(key, error) for key in SIZE_KEYS}
    return {**names, **sizes}


__all__ = ["FONT_ENV", "FONT_KEYS", "SIZE_KEYS", "font_name", "font_size", "normalisers"]
