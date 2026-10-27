"""Which font the PDF converter would really use for a Word font family (fontconfig).

LibreOffice asks fontconfig for the fonts a Word document names. Calibri, Aptos and Cambria are
not installed on a Linux host, so without the metric-compatible Carlito and Caladea fonts a wider
fallback (DejaVu) is used and every line break and page number changes. ``resolve_family`` asks
``fc-match`` for the answer: a fixed argument list (no shell), a short timeout, and ``None`` for
anything that does not give a clear answer, so a missing fontconfig is never an error.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess  # noqa: S404 - fixed argument list, no shell, see resolve_family
from pathlib import Path

log = logging.getLogger(__name__)

FC_MATCH = "fc-match"
PROBE_TIMEOUT = 5.0
FAMILY = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")
FAMILY_FORMAT = "%{family}"


def resolve_family(family: str, conf: Path | None = None) -> str | None:
    """The family fontconfig substitutes for ``family`` (the first of its names), or None.

    With ``conf`` the lookup runs under that fontconfig file (the renderer's generated one)."""
    if not FAMILY.fullmatch(family):
        return None
    binary = shutil.which(FC_MATCH)
    if binary is None:
        return None
    extra = {} if conf is None else {"env": {**os.environ, "FONTCONFIG_FILE": str(conf)}}
    try:
        done = subprocess.run(  # noqa: S603 - argument list, family validated above
            [binary, "-f", FAMILY_FORMAT, family],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            check=False,
            shell=False,
            **extra,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("fc-match failed (%s)", type(exc).__name__)
        return None
    first = done.stdout.split(",")[0].strip() if done.returncode == 0 else ""
    return first[:100] or None
