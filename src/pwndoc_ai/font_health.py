"""The ``FONTS_CATALOG`` setup check: which open catalog fonts are missing, and how to install them.

Kept out of health.py so that file does not grow; health.py adds one builder line. The detail
names the install command and is shown to administrators only (the /health route is admin-only).
A family the resolver knows nothing about (``None``) is "unknown", never reported as missing:
a host without fontconfig gets no false alarm.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from .font_catalog import CATALOG, default_fonts_dir, dir_families
from .font_catalog_model import squash

if TYPE_CHECKING:
    from .health import Check

CHECK_ID = "FONTS_CATALOG"
LABEL = "Font catalog"
INSTALL_COMMAND = "sudo scripts/install_fonts.sh"
RESTART_HINT = "then restart the gateway"
MAX_LISTED = 12


def missing_families(
    installed: frozenset[str], resolver: Callable[[str], str | None]
) -> tuple[str, ...]:
    """Open catalog families that are not installed and that fontconfig replaces."""
    present = {squash(name) for name in installed}
    missing = []
    for entry in CATALOG:
        if entry.proprietary or squash(entry.family) in present:
            continue
        answer = resolver(entry.family)
        if answer and squash(answer) != squash(entry.family):
            missing.append(entry.family)
    return tuple(missing)


def _project_installed(fonts_dir: Path | None) -> frozenset[str]:
    try:
        return dir_families(fonts_dir)
    except OSError:
        return frozenset()


def check_font_catalog(
    installed: frozenset[str] | None,
    resolver: Callable[[str], str | None],
    *,
    fonts_dir: Path | None = None,
) -> Check:
    """OK when every open catalog font is installed (or cannot be checked); WARN listing the
    missing families and the install command otherwise. ``installed=None`` means: the families
    in the project fonts directory (``$PWNDOC_AI_FONTS_DIR`` / ``<data_dir>/fonts``)."""
    from .health import OK, WARN, Check

    if installed is None:
        directory = fonts_dir if fonts_dir is not None else default_fonts_dir(os.environ)
        installed = _project_installed(directory)
    missing = missing_families(installed, resolver)
    if not missing:
        return Check(CHECK_ID, LABEL, OK, "Every open catalog font is installed or not checked")
    shown = ", ".join(missing[:MAX_LISTED])
    more = f" and {len(missing) - MAX_LISTED} more" if len(missing) > MAX_LISTED else ""
    detail = (
        f"Not installed, a substitute is used: {shown}{more}. "
        f"Install with: {INSTALL_COMMAND} ({RESTART_HINT})"
    )
    return Check(CHECK_ID, LABEL, WARN, detail)


__all__ = ["CHECK_ID", "INSTALL_COMMAND", "check_font_catalog", "missing_families"]
