"""AI steps of the normalisation (docs/FINDING_ORDER.md B4 step 4 and B9 step 1).

* ``LlmCvssEstimator`` asks the LLM for the eight CVSS 3.1 base metrics with a strict tool
  schema (each metric an enum of its letters). The prompt is masked like the refiner's
  (``prepare_corpus`` before the first stand-in, ``mask_plain``), the answer is validated and
  the score is computed by ``pwndoc_ai.cvss`` only. Any failure returns ``None`` so the caller
  falls back to the deterministic heuristic.
* ``accept_fill`` validates what a *filler* (the gateway's refiner in ``fill_missing`` mode)
  returned for the empty fields: unrequested keys are dropped, and each value must pass its
  per-field rule or it is discarded (the field then goes to the deterministic fallback / N/A).
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .cvss import ORDER, PREFIX, parse_vector
from .finding_fields import (
    CVE_LIST,
    CWE_LIST,
    RICH_FIELDS,
    cve_in,
    finding_text,
    is_placeholder,
)
from .stand_ins import prepare_corpus, stray_stand_ins

log = logging.getLogger(__name__)

METRIC_VALUES = {
    "AV": ("N", "A", "L", "P"),
    "AC": ("L", "H"),
    "PR": ("N", "L", "H"),
    "UI": ("N", "R"),
    "S": ("U", "C"),
    "C": ("H", "L", "N"),
    "I": ("H", "L", "N"),
    "A": ("H", "L", "N"),
}
ESTIMATE_INPUT_KEYS = ("title", "description", "observation", "poc", "affected", "cwe")
MAX_PROMPT_FIELD = 4000
TOOL_NAME = "cvss_base_metrics"
SYSTEM_PROMPT = (
    "You are a penetration-testing report reviewer. Estimate the CVSS v3.1 base metrics of the "
    "finding from its text. Choose one letter per metric; do not compute a score."
)
FILLABLE = (
    "description",
    "observation",
    "poc",
    "steps",
    "remediation",
    "references",
    "location",
    "path",
    "parameter",
    "affected",
    "cwe",
    "cve",
    "cvss_vector",
)
STATED_FIELDS = ("location", "path", "parameter", "affected")
_URL = re.compile(r"^https?://[^\s<>\"']{1,2000}$", re.IGNORECASE)
Filler = Callable[[Mapping[str, Any], tuple[str, ...]], Any]
Estimator = Callable[[Mapping[str, Any]], Any]


def build_metrics_tool() -> dict[str, Any]:
    props = {k: {"type": "string", "enum": list(v)} for k, v in METRIC_VALUES.items()}
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "Return the eight CVSS v3.1 base metrics of the finding.",
            "parameters": {
                "type": "object",
                "properties": props,
                "required": list(ORDER),
                "additionalProperties": False,
            },
        },
    }


def vector_from_metrics(args: Any) -> str | None:
    """The canonical CVSS:3.1 vector of a valid metrics answer, else None."""
    if not isinstance(args, Mapping):
        return None
    letters = {k: args.get(k) for k in ORDER}
    if any(not isinstance(v, str) or v not in METRIC_VALUES[k] for k, v in letters.items()):
        return None
    parsed = parse_vector(PREFIX + "/".join(f"{k}:{letters[k]}" for k in ORDER))
    return None if parsed is None else parsed.vector


def _bounded(value: Any) -> str:
    return value[:MAX_PROMPT_FIELD] if isinstance(value, str) else ""


class LlmCvssEstimator:
    """``estimator(finding) -> 'CVSS:3.1/...' | None``; ``llm`` has ``call_tool``."""

    def __init__(self, llm: Any, masker: Any = None) -> None:
        self._llm = llm
        self._masker = masker

    def _mask(self, text: str) -> str:
        if self._masker is None:
            return text
        mask = getattr(self._masker, "mask_plain", None) or self._masker.mask
        return mask(text)

    def _user_message(self, finding: Mapping[str, Any]) -> tuple[str, list[str]]:
        originals = [_bounded(finding.get(k)) for k in ESTIMATE_INPUT_KEYS]
        prepare_corpus(self._masker, originals)  # before the first stand-in is issued
        parts = [
            f"<{key}>\n{self._mask(text)}\n</{key}>"
            for key, text in zip(ESTIMATE_INPUT_KEYS, originals, strict=True)
            if text and not is_placeholder(text)
        ]
        return "\n".join(parts), originals

    def __call__(self, finding: Mapping[str, Any]) -> str | None:
        try:
            user, originals = self._user_message(finding)
            if not user:
                return None
            args, _usage = self._llm.call_tool(SYSTEM_PROMPT, user, build_metrics_tool())
            if self._masker is not None and stray_stand_ins(self._masker, args, originals):
                return None
            return vector_from_metrics(args)
        except Exception as exc:  # noqa: BLE001 - any failure falls back to the heuristic
            log.warning("CVSS estimation by the LLM failed (%s)", type(exc).__name__)
            return None


@dataclass(frozen=True)
class FillResult:
    values: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, list[str]] = field(default_factory=dict)


def _rich(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "".join(f"<p>{html.escape(line)}</p>" for line in lines)


def _urls(value: Any) -> list[str]:
    items = value if isinstance(value, (list, tuple)) else str(value or "").split()
    return [str(u).strip() for u in items if isinstance(u, str) and _URL.match(u.strip())]


def _accept_one(name: str, value: Any, finding: Mapping, sources: list[str]) -> Any:
    if name == "references":
        allowed = set(sources)
        kept = [u for u in _urls(value) if u in allowed]
        return "\n".join(dict.fromkeys(kept)) or None
    if not isinstance(value, str) or is_placeholder(value):
        return None
    text = value.strip()
    if name in RICH_FIELDS:
        return _rich(text) or None
    if name in STATED_FIELDS:
        return text if text.lower() in finding_text(finding).lower() else None
    if name == "cwe":
        return text.upper() if CWE_LIST.match(text.upper()) else None
    if name == "cve":
        return _accept_cve(text, finding, sources)
    if name == "cvss_vector":
        parsed = parse_vector(text)
        return parsed.vector if parsed is not None else None
    return None


def _accept_cve(text: str, finding: Mapping, sources: list[str]) -> str | None:
    ids = cve_in(text)
    if not ids or not CVE_LIST.match(", ".join(ids)):
        return None
    stated = set(cve_in(finding_text(finding)))
    if sources:  # named by a fetched source the filler matched to this finding
        return ", ".join(ids)
    kept = [c for c in ids if c in stated]
    return ", ".join(kept) or None


def _normalise_raw(raw: Any) -> FillResult:
    if isinstance(raw, FillResult):
        return raw
    if not isinstance(raw, Mapping):
        return FillResult()
    values = raw.get("values", raw)
    sources = raw.get("sources", {}) if "values" in raw else {}
    return FillResult(
        dict(values) if isinstance(values, Mapping) else {},
        dict(sources) if isinstance(sources, Mapping) else {},
    )


def accept_fill(raw: Any, finding: Mapping[str, Any], asked: Sequence[str]) -> FillResult:
    """Keep only valid answers for the asked fields, with their source URLs."""
    result = _normalise_raw(raw)
    values: dict[str, Any] = {}
    sources: dict[str, list[str]] = {}
    for name in asked:
        if name not in FILLABLE or name not in result.values:
            continue
        field_sources = [str(u) for u in result.sources.get(name, []) or [] if isinstance(u, str)]
        accepted = _accept_one(name, result.values[name], finding, field_sources)
        if accepted is not None:
            values[name] = accepted
            sources[name] = field_sources
    return FillResult(values, sources)


def run_filler(filler: Filler | None, finding: Mapping, asked: tuple[str, ...]) -> FillResult:
    """Call the filler (B9 step 1); a failure or nothing valid yields an empty result."""
    if filler is None or not asked:
        return FillResult()
    try:
        raw = filler(finding, asked)
    except Exception as exc:  # noqa: BLE001 - the deterministic fallback takes over
        log.warning("AI fill of empty fields failed (%s)", type(exc).__name__)
        return FillResult()
    return accept_fill(raw, finding, asked)
