"""Refines a PwnDoc finding with an LLM and produces a safe, minimal update payload.

Guarantees, regardless of what the model returns:
* output text is passed through the HTML allow-list sanitiser;
* ``<img>`` evidence tags present in the original are never lost;
* custom fields are matched by label onto the ORIGINAL field objects (ids are never model-made),
  select/radio values must be one of the field's options, and redacted labels are untouched;
* only fields that actually changed are placed in the payload.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .llm import LlmClient, Usage
from .prompts import ALL_FIELDS, RICH_FIELDS, build_system_prompt, build_tool
from .sanitize import image_tags, preserve_images, to_rich_text

UPDATABLE_KEYS = (
    "title", "vulnType", "description", "observation", "remediation", "remediationComplexity",
    "priority", "references", "cvssv3", "cvssv4", "poc", "scope", "status", "category",
    "customFields",
)  # fmt: skip
MAX_REFERENCES = 40
MAX_TITLE = 200
REQUEST_KEYS = ("title", "vulnType", "description", "observation", "poc", "scope",
                "remediation", "references", "cvssv3", "category")  # fmt: skip
TEXT_TYPES = {"input", "text", "select", "radio"}
SAFE_REFERENCE = re.compile(r"^(https?://\S+|(CWE|CVE)-[\d-]+.*)$", re.IGNORECASE)
MAX_REFERENCE_LEN = 300
ANGLE = re.compile(r"[<>]")
TAG = re.compile(r"<[^>]+>")
BLOCK_END = re.compile(r"</(p|li|h[1-6]|pre|blockquote|tr)>|<br\s*/?>", re.IGNORECASE)


@dataclass(frozen=True)
class RefineOptions:
    fields: tuple[str, ...] = ALL_FIELDS
    mode: str = "improve"  # "improve" rewrites; "fill_missing" only fills empty fields


@dataclass(frozen=True)
class Proposal:
    finding_id: str
    title: str
    payload: dict[str, Any]
    changes: dict[str, dict[str, str]]
    usage: Usage = field(default_factory=Usage)


# -- small helpers -----------------------------------------------------------------------------
def to_plain_text(value: Any) -> str:
    text = BLOCK_END.sub("\n", str(value or ""))
    return html.unescape(TAG.sub("", text)).strip()


def to_safe_line(value: Any) -> str:
    """Single-line plain text with no angle brackets (for title / input-type fields)."""
    return re.sub(r"\s+", " ", ANGLE.sub("", to_plain_text(value))).strip()


def _is_empty(value: Any) -> bool:
    if isinstance(value, list):
        return not [v for v in value if str(v).strip()]
    return not to_plain_text(value) and "<img" not in str(value or "")


def _label(custom: dict[str, Any]) -> str:
    return str(custom.get("label") or custom.get("display") or "").strip()


def _option_values(custom: dict[str, Any]) -> list[str]:
    return [str(o.get("value", "")) for o in custom.get("options") or [] if isinstance(o, dict)]


def finding_update_payload(finding: dict[str, Any]) -> dict[str, Any]:
    """The subset of a finding that PwnDoc's PUT endpoint accepts."""
    return {k: finding[k] for k in UPDATABLE_KEYS if finding.get(k) not in (None, "")}


def apply_payload(finding: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return {**finding, **payload}


# -- custom fields -----------------------------------------------------------------------------
def _custom_entries(finding: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in finding.get("customFields") or [] if isinstance(e, dict)]


def _entry_custom(entry: dict[str, Any]) -> dict[str, Any]:
    custom = entry.get("customField")
    return custom if isinstance(custom, dict) else {}


def _describe_custom_fields(
    finding: dict[str, Any], redacted: set[str], only_empty: bool
) -> list[dict[str, Any]]:
    described = []
    for entry in _custom_entries(finding):
        custom = _entry_custom(entry)
        label = _label(custom)
        if not label or custom.get("fieldType") not in TEXT_TYPES:
            continue
        if label.lower() in redacted or (only_empty and not _is_empty(entry.get("text"))):
            continue
        described.append(
            {
                "label": label,
                "type": custom.get("fieldType"),
                "allowed_values": _option_values(custom) or None,
                "hint": custom.get("description") or None,
                "current": to_plain_text(entry.get("text")),
            }
        )
    return described


def _coerce_custom_value(custom: dict[str, Any], value: Any) -> str | None:
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    options = _option_values(custom)
    if options:
        by_lower = {o.lower(): o for o in options}
        return by_lower.get(text.lower())
    if custom.get("fieldType") == "text":
        return to_rich_text(text)
    return to_safe_line(text)


def _merge_custom_fields(
    finding: dict[str, Any], model_values: Any, allowed_labels: set[str]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    wanted = {
        str(item.get("label", "")).strip().lower(): item.get("value")
        for item in model_values or []
        if isinstance(item, dict)
    }
    merged: list[dict[str, Any]] = []
    changes: dict[str, dict[str, str]] = {}
    for entry in _custom_entries(finding):
        custom = _entry_custom(entry)
        label = _label(custom)
        key = label.lower()
        new_text = _coerce_custom_value(custom, wanted.get(key)) if key in allowed_labels else None
        if new_text is not None and to_plain_text(new_text) != to_plain_text(entry.get("text")):
            changes[f"customFields: {label}"] = {
                "before": to_plain_text(entry.get("text")),
                "after": to_plain_text(new_text),
            }
            merged.append({**entry, "text": new_text})
        else:
            merged.append(entry)
    return merged, changes


# -- the refiner -------------------------------------------------------------------------------
class FindingRefiner:
    def __init__(
        self,
        llm: LlmClient,
        settings: Settings,
        context: Callable[[str], str] | None = None,
    ):
        self._llm = llm
        self._settings = settings
        self._context = context

    def close(self) -> None:
        """Release the LLM client's connection pool (jobs call this when they finish)."""
        close = getattr(self._llm, "close", None)
        if callable(close):
            close()

    def refine(self, finding: dict[str, Any], options: RefineOptions | None = None) -> Proposal:
        opts = options or RefineOptions()
        redacted = {label.lower() for label in self._settings.redact_field_labels}
        only_empty = opts.mode == "fill_missing"
        targets = self._target_fields(finding, opts, only_empty)
        custom = (
            _describe_custom_fields(finding, redacted, only_empty)
            if "customFields" in opts.fields
            else []
        )
        finding_id = str(finding.get("_id") or finding.get("id") or "")
        title = str(finding.get("title", ""))
        if not targets and not custom:
            return Proposal(finding_id, title, {}, {})
        args, usage = self._ask(finding, targets, custom)
        payload, changes = self._build_changes(finding, args, targets, custom)
        return Proposal(finding_id, title, payload, changes, usage)

    @staticmethod
    def _target_fields(
        finding: dict[str, Any], opts: RefineOptions, only_empty: bool
    ) -> tuple[str, ...]:
        selected = [f for f in opts.fields if f != "customFields"]
        if only_empty:
            selected = [f for f in selected if _is_empty(finding.get(f))]
        return tuple(selected)

    def _ask(
        self, finding: dict[str, Any], targets: tuple[str, ...], custom: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], Usage]:
        draft = {k: finding.get(k) for k in REQUEST_KEYS if finding.get(k) not in (None, "")}
        research = self._context(str(finding.get("title", ""))) if self._context else ""
        parts = [
            "Rewrite the following draft finding. Fields to return: "
            + ", ".join([*targets, *(["custom_fields"] if custom else [])]),
            "DRAFT FINDING (JSON):\n" + json.dumps(draft, indent=2, ensure_ascii=False),
        ]
        if custom:
            parts.append(
                "CUSTOM FIELDS TO FILL (JSON):\n" + json.dumps(custom, indent=2, ensure_ascii=False)
            )
        if research:
            parts.append(f"<research>\n{research}\n</research>")
        tool_fields = (*targets, *(["customFields"] if custom else []))
        tool = build_tool(tuple(tool_fields), bool(custom))
        system = build_system_prompt(self._settings.extra_instructions)
        return self._llm.call_tool(system, "\n\n".join(parts), tool)

    def _build_changes(
        self,
        finding: dict[str, Any],
        args: dict[str, Any],
        targets: tuple[str, ...],
        custom: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
        payload: dict[str, Any] = {}
        changes: dict[str, dict[str, str]] = {}
        # The model may only place evidence images that already exist somewhere in this finding.
        allowed_images = frozenset(
            src for name in RICH_FIELDS for src in image_tags(str(finding.get(name) or ""))
        )

        def record(name: str, new: Any, before: str, after: str) -> None:
            if after.strip() and after.strip() != before.strip():
                payload[name] = new
                changes[name] = {"before": before, "after": after}

        for name in targets:
            if name == "references":
                merged = self._merge_references(finding.get("references"), args.get("references"))
                record(name, merged, "\n".join(finding.get("references") or []), "\n".join(merged))
            elif name == "title":
                new = to_safe_line(args.get("title"))[:MAX_TITLE]
                record(name, new, str(finding.get("title") or ""), new)
            elif name in RICH_FIELDS:
                original = str(finding.get(name) or "")
                new = preserve_images(
                    original,
                    to_rich_text(str(args.get(name) or ""), allowed_images),
                )
                record(name, new, to_plain_text(original), to_plain_text(new))
        if custom:
            labels = {c["label"].lower() for c in custom}
            merged_cf, cf_changes = _merge_custom_fields(finding, args.get("custom_fields"), labels)
            if cf_changes:
                payload["customFields"] = merged_cf
                changes.update(cf_changes)
        return payload, changes

    @staticmethod
    def _merge_references(original: Any, model: Any) -> list[str]:
        def clean(items: Any) -> list[str]:
            if isinstance(items, str):
                items = items.splitlines()
            return [s for s in (str(i).strip() for i in items or []) if s]

        def acceptable(ref: str) -> bool:
            return len(ref) <= MAX_REFERENCE_LEN and bool(SAFE_REFERENCE.match(ref))

        model_refs = [ref for ref in clean(model) if acceptable(ref)]
        merged = list(dict.fromkeys([*clean(original), *model_refs]))
        return merged[:MAX_REFERENCES]
