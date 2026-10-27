"""Layout profiles of the deterministic reader (no lxml, no package imports: a leaf module).

The reader was written for one template ("default"). A layout profile switches on the few
behaviours another template needs, without changing the default: ``read_report`` detects the
profile once per document and makes it active through a ``ContextVar`` (like the budget of
``reader_xml``), so the leaf functions (``labels.field_of``, ``findings.parse_blob``,
caption pairing, ``dedupe``) read it without new parameters. Outside ``use`` the default
profile is active, so every existing caller and test keeps its behaviour.

Profiles:

* ``default``: the original PwnDoc template (brand / demo samples).
* ``paradigm``: the ParadigmIT VAPT report layout (cover card "PREPARED FOR" / "ASSESSMENT
  WINDOW", register "List of Vulnerable Parameter / Location Discovered", 13-row detail tables
  with "Steps to Reproduce", "Representative example - host" captions).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True)
class LayoutProfile:
    name: str
    field_labels: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    steps_label: str = ""  # PoC lead-in for the "steps" field ("" = no steps field)
    free_captions: bool = False  # a short plain line right after a picture is its caption
    blob_lead_urls: bool = False  # register text before the first label is the URL list
    blob_param_label: bool = False  # "Param:" is a register label
    detail_wins: bool = False  # detail copy's location/parameter/urls beat the register's
    join_wrapped_urls: bool = False  # a URL line not starting a URL continues the previous
    continuation_rows: bool = False  # an unlabelled 1-cell detail row continues the field above
    end_markers: tuple[str, ...] = ()  # label keys of headings that close the finding region
    severity_fills: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


DEFAULT = LayoutProfile(name="default")
PARADIGM = LayoutProfile(
    name="paradigm",
    field_labels=MappingProxyType({"stepstoreproduce": "steps"}),
    steps_label="Steps to reproduce:",
    free_captions=True,
    blob_lead_urls=True,
    blob_param_label=True,
    detail_wins=True,
    join_wrapped_urls=True,
    continuation_rows=True,
    end_markers=("07disclaimer", "disclaimer", "theexpertisebehindtheassessment"),
    severity_fills=MappingProxyType(
        {
            "FBE5E5": "Critical",
            "FCEBDD": "High",
            "FBF4DD": "Medium",
            "E8F0F9": "Low",
            "E3F4FA": "Informational",
        }
    ),
)
PROFILES = {DEFAULT.name: DEFAULT, PARADIGM.name: PARADIGM}

# Markers of the Paradigm layout, as label keys (letters and digits only). A marker counts only
# when a whole short paragraph / cell / header line IS the label (a leading section number
# like "6." is ignored), so prose such as "prepared for the team" never fires it. Two are
# needed, one of them strong (the running header or the findings heading).
PARADIGM_MARKERS = (
    "preparedfor",
    "assessmentwindow",
    "detailedtechnicalfindings",
    "paradigmitcybersecurityvapt",
    "representativeexample",
)
PARADIGM_STRONG = frozenset({"paradigmitcybersecurityvapt", "detailedtechnicalfindings"})
# the caption ("... - example.com" follows) and the running header (a logo text may follow)
PARADIGM_PREFIX_MARKERS = frozenset({"representativeexample", "paradigmitcybersecurityvapt"})
PARADIGM_MIN_MARKERS = 2
MAX_LABEL_CHARS = 60

_active: ContextVar[LayoutProfile] = ContextVar("docx_import_profile", default=DEFAULT)
_NON_ALNUM = re.compile(r"[^a-z0-9]")


def active() -> LayoutProfile:
    """The profile of the document being read (``DEFAULT`` outside ``use``)."""
    return _active.get()


@contextmanager
def use(profile: LayoutProfile) -> Iterator[LayoutProfile]:
    token = _active.set(profile)
    try:
        yield profile
    finally:
        _active.reset(token)


def _key(text: str) -> str:
    return _NON_ALNUM.sub("", str(text).casefold())


def _marker(text: str) -> str:
    """The marker a label-like text is, or ``""``."""
    if len(" ".join(str(text).split())) > MAX_LABEL_CHARS:
        return ""
    key = _key(text).lstrip("0123456789")
    if key in PARADIGM_MARKERS:
        return key
    return next((m for m in PARADIGM_PREFIX_MARKERS if key.startswith(m)), "")


def detect(texts: Iterator[str] | list[str]) -> LayoutProfile:
    """The profile whose markers the document's texts (body paragraphs, cells, page header)
    show; ``DEFAULT`` otherwise."""
    found: set[str] = set()
    for text in texts:
        marker = _marker(text)
        if marker:
            found.add(marker)
        if len(found) >= PARADIGM_MIN_MARKERS and found & PARADIGM_STRONG:
            return PARADIGM
    return DEFAULT


__all__ = ["DEFAULT", "PARADIGM", "PROFILES", "LayoutProfile", "active", "detect", "use"]
