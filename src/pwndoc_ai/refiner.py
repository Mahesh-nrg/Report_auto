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
import inspect
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Protocol

from .config import Settings
from .cvss import Cvss, is_score_label, is_severity_label, parse_vector
from .fill_guard import guard_fill, is_placeholder
from .llm import LlmClient, Usage
from .prompts import ALL_FIELDS, RICH_FIELDS, build_system_prompt, build_tool
from .sanitize import image_tags, preserve_images, to_rich_text
from .stand_ins import prepare_corpus, redact_text, stray_stand_ins

UPDATABLE_KEYS = (
    "title", "vulnType", "description", "observation", "remediation", "remediationComplexity",
    "priority", "references", "cvssv3", "cvssv4", "poc", "scope", "status", "category",
    "customFields",
)  # fmt: skip
MAX_REFERENCES = 40
MAX_TITLE = 200
REQUEST_KEYS = ("title", "vulnType", "description", "observation", "poc", "scope",
                "remediation", "references", "cvssv3", "category")  # fmt: skip
EXCERPT_KEYS = ("description", "observation")  # draft fields that steer passage ranking
TEXT_TYPES = {"input", "text", "select", "radio"}
SAFE_REFERENCE = re.compile(r"^(https?://\S+|(CWE|CVE)-[\d-]+.*)$", re.IGNORECASE)
MAX_REFERENCE_LEN = 300
LIBRARY_LOCALE = "en"
DERIVED_FIELD_TYPES = frozenset({"input", "select", "radio"})  # never a free-text "text" field
log = logging.getLogger(__name__)
ANSWER_KEYS = ("title", *RICH_FIELDS, "references", "custom_fields", "cvss_vector")
NO_SEVERITY_OPTIONS = ("informational", "info", "none")  # where a 0.0 score is filed
ANGLE = re.compile(r"[<>]")
TAG = re.compile(r"<[^>]+>")
BLOCK_END = re.compile(r"</(p|li|h[1-6]|pre|blockquote|tr)>|<br\s*/?>", re.IGNORECASE)


class MaskerLike(Protocol):
    """What the refiner needs from a masker.

    ``mask_plain``, ``mask_json_plain``, ``leftovers``, ``prepare``, ``redact`` and
    ``dummy_leaks`` are used when present (the real ``Masker`` has them; a minimal test double
    need not).
    """

    def mask(self, text: str) -> str: ...

    def mask_json(self, value: Any) -> Any: ...

    def unmask(self, value: Any) -> Any: ...


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
    model: str = ""  # the model that actually answered (after any fallback)
    sources: tuple[str, ...] = ()  # documents / hostnames the research context came from
    ai_filled: tuple[str, ...] = ()  # fill_missing mode: the empty fields the AI filled (B9)


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


def _is_missing(value: Any) -> bool:
    """Empty, or a placeholder such as '-', 'none' or 'N/A' (docs/FINDING_ORDER.md B2)."""
    return _is_empty(value) or is_placeholder(value)


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
        if label.lower() in redacted or (only_empty and not _is_missing(entry.get("text"))):
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


def _derived_kind(custom: dict[str, Any], label: str) -> str | None:
    """``score`` / ``severity`` for the field that holds the computed CVSS value, else ``None``."""
    if custom.get("fieldType") not in DERIVED_FIELD_TYPES:
        return None
    if is_score_label(label):
        return "score"
    return "severity" if is_severity_label(label) else None


def _field_value(custom: dict[str, Any], label: str, model_value: Any, cvss: Cvss | None) -> Any:
    """Score and severity fields are computed from the vector, never taken from the model.

    Without any valid vector they are left alone. A computed severity that is not one of a select
    field's options falls back to the model's pick, except "None" (score 0.0): that goes to an
    Informational/Info/None option, and when the field has none the field is left alone (the
    model's "Critical" must never be written next to a 0.0 score).
    """
    kind = _derived_kind(custom, label)
    if kind is None:
        return model_value
    if cvss is None:
        return None
    if kind == "score":
        return f"{cvss.score:.1f}"
    if cvss.severity == "None":
        return _no_severity_value(custom)
    computed = cvss.severity
    return computed if _coerce_custom_value(custom, computed) is not None else model_value


def _no_severity_value(custom: dict[str, Any]) -> str | None:
    """The option that means "no severity" (free-text fields get the word ``None``)."""
    options = _option_values(custom)
    if not options:
        return "None"
    by_lower = {o.lower(): o for o in options}
    return next((by_lower[a] for a in NO_SEVERITY_OPTIONS if a in by_lower), None)


def _merge_custom_fields(
    finding: dict[str, Any],
    model_values: Any,
    allowed_labels: set[str],
    cvss: Cvss | None = None,
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
        new_text = None
        if key in allowed_labels:
            value = _field_value(custom, label, wanted.get(key), cvss)
            new_text = _coerce_custom_value(custom, value)
        if new_text is not None and to_plain_text(new_text) != to_plain_text(entry.get("text")):
            changes[f"customFields: {label}"] = {
                "before": to_plain_text(entry.get("text")),
                "after": to_plain_text(new_text),
            }
            merged.append({**entry, "text": new_text})
        else:
            merged.append(entry)
    return merged, changes


# -- optional collaborators (masker / library) ---------------------------------------------------
def library_reference(title: str, items: Sequence[Mapping], locale: str) -> str:
    """Approved library wording for ``title``; imported lazily so the module stays optional."""
    try:
        from .library import reference_for
    except ImportError:
        return ""
    return str(reference_for(title, items, locale) or "")


def _neutralise(text: str, tag: str) -> str:
    """Stop untrusted text from opening or closing its own ``<tag>`` wrapper."""
    return re.sub(rf"<(/?)\s*{tag}", rf"<\1 {tag}", text, flags=re.IGNORECASE)


def _sources_of(research: Any) -> tuple[str, ...]:
    sources = getattr(research, "sources", ())
    if isinstance(sources, str) or not hasattr(sources, "__iter__"):
        return ()
    return tuple(str(s) for s in sources)


class _Request(NamedTuple):
    system: str
    user: str
    tool: dict[str, Any]
    sources: tuple[str, ...]
    originals: Any  # the unmasked text that was sent, to tell the model's tokens from real ones


def _takes_excerpt(provider: Any) -> bool:
    """True when the context provider accepts the optional draft excerpt as a second argument."""
    try:
        params = inspect.signature(provider).parameters.values()
    except (TypeError, ValueError):
        return False
    if any(p.kind is p.VAR_POSITIONAL for p in params):
        return True
    return sum(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in params) >= 2


# -- the refiner -------------------------------------------------------------------------------
class FindingRefiner:
    def __init__(
        self,
        llm: LlmClient,
        settings: Settings,
        context: Callable[[str], str] | None = None,
        masker: MaskerLike | None = None,
        library: Sequence[Mapping] | None = None,
        library_locale: str = LIBRARY_LOCALE,
    ) -> None:
        self._llm = llm
        self._settings = settings
        self._context = context
        self._masker = masker
        self._library: tuple[Mapping, ...] = tuple(library or ())
        self._library_locale = library_locale or LIBRARY_LOCALE

    def use_masker(self, masker: MaskerLike | None) -> None:
        """Per-job injection: an object with ``mask``, ``mask_json`` and ``unmask`` methods."""
        self._masker = masker

    def use_library(self, items: Sequence[Mapping] | None, locale: str | None = None) -> None:
        """Per-job injection of the approved library entries (may be empty) and their language."""
        self._library = tuple(items or ())
        self._library_locale = locale or LIBRARY_LOCALE

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
        request = self._build_request(finding, targets, custom, only_empty)
        args, usage = self._llm.call_tool(request.system, request.user, request.tool)
        args = self._unmask({k: args[k] for k in ANSWER_KEYS if k in args})
        self._reject_stray_stand_ins(args, request.originals)
        payload, changes = self._build_changes(finding, args, targets, custom, only_empty)
        filled: tuple[str, ...] = ()
        if only_empty:
            research = str(request.originals[2]) if len(request.originals) > 2 else ""
            payload, changes = guard_fill(finding, payload, changes, research, request.sources)
            filled = tuple(changes)
        model = str(getattr(self._llm, "last_model", "") or "")
        return Proposal(finding_id, title, payload, changes, usage, model, request.sources, filled)

    def _unmask(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._masker is None:
            return args
        try:
            restored = self._masker.unmask(args)
        except ValueError as exc:  # too deeply nested: it cannot be checked, so it is not used
            raise ValueError(f"the model's answer cannot be used: {exc}") from exc
        if not isinstance(restored, dict):
            raise TypeError("masker.unmask must return a dict for the model's arguments")
        return restored

    def _reject_stray_stand_ins(self, args: dict[str, Any], originals: Any) -> None:
        """Fail instead of writing a stand-in value (a dummy name, host or address the masker
        could not restore, or a legacy ``[[CLIENT_7]]`` token) into a report.

        Values that were already literally present in the text we sent are not the model's
        doing. The message names no value: a stand-in must never reach a log or the UI.
        """
        try:
            stray = stray_stand_ins(self._masker, args, originals)
        except ValueError as exc:  # too deeply nested: unscannable, so not trusted
            raise ValueError(f"the model's answer cannot be checked: {exc}") from exc
        if stray:
            raise ValueError(
                f"the model returned {len(stray)} placeholder or stand-in value(s) that cannot "
                "be restored"
            )

    @staticmethod
    def _target_fields(
        finding: dict[str, Any], opts: RefineOptions, only_empty: bool
    ) -> tuple[str, ...]:
        selected = [f for f in opts.fields if f != "customFields"]
        if only_empty:
            selected = [f for f in selected if _is_missing(finding.get(f))]
        return tuple(selected)

    def _mask(self, text: str) -> str:
        """Mask plain text (research, library, custom values): ``<`` is not markup there."""
        if self._masker is None:
            return text
        mask = getattr(self._masker, "mask_plain", None) or self._masker.mask
        return mask(text)

    def _mask_draft(self, draft: dict[str, Any]) -> dict[str, Any]:
        """Rich-text fields are masked as HTML, every other field as plain text."""
        plain = getattr(self._masker, "mask_json_plain", None) or self._masker.mask_json
        return {
            key: (self._masker.mask_json(value) if key in RICH_FIELDS else plain(value))
            for key, value in draft.items()
        }

    def _research_query(self, title: str) -> str:
        """The finding title as it may leave the network: client data removed, not replaced by
        stand-ins (a web search must see neither the real nor the dummy values)."""
        if self._masker is None:
            return title
        redacted = redact_text(self._masker, title)
        if redacted == title:
            return title
        return " ".join(ANGLE.sub(" ", redacted).split())

    def _research(self, query: str, draft: Mapping[str, Any]) -> str:
        """Run the context provider; the redacted draft text only ever ranks passages locally."""
        excerpt = " ".join(str(draft.get(key) or "") for key in EXCERPT_KEYS).strip()
        excerpt = redact_text(self._masker, excerpt)
        if excerpt and _takes_excerpt(self._context):
            return self._context(query, excerpt)  # type: ignore[misc]
        return self._context(query)  # type: ignore[misc]

    def _library_text(self, title: str, finding_id: str) -> str:
        if not self._library or not self._settings.use_library:
            return ""
        try:
            return library_reference(title, self._library, self._library_locale).strip()
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            # The title is client data and the error text may quote it: log neither.
            log.warning(
                "vulnerability library lookup failed for finding %s (%s)",
                finding_id or "?",
                type(exc).__name__,
            )
            return ""

    def _build_request(
        self,
        finding: dict[str, Any],
        targets: tuple[str, ...],
        custom: list[dict[str, Any]],
        fill: bool = False,
    ) -> _Request:
        title = str(finding.get("title", ""))
        draft = {k: finding.get(k) for k in REQUEST_KEYS if finding.get(k) not in (None, "")}
        originals: list[Any] = [draft, [c["current"] for c in custom]]
        prepare_corpus(self._masker, [title, *originals])  # before the first stand-in is issued
        query = self._research_query(title)
        research = self._research(query, draft) if self._context and query else ""
        finding_id = str(finding.get("_id") or finding.get("id") or "")
        library_text = self._library_text(title, finding_id)
        references = (str(research or ""), library_text)
        originals += references
        if self._masker is not None:
            prepare_corpus(self._masker, list(references))
            draft = self._mask_draft(draft)
            custom = [{**c, "current": self._mask(c["current"])} for c in custom]
        user = self._user_message(targets, draft, custom, references, fill)
        tool_fields = (*targets, *(["customFields"] if custom else []))
        tool = build_tool(tuple(tool_fields), bool(custom))
        system = build_system_prompt(self._settings.extra_instructions)
        return _Request(system, user, tool, _sources_of(research), originals)

    def _user_message(
        self,
        targets: tuple[str, ...],
        draft: dict[str, Any],
        custom: list[dict[str, Any]],
        references: tuple[str, str],
        fill: bool = False,
    ) -> str:
        """The (already masked) draft and custom fields plus the masked research/library text."""
        parts = [
            (
                "Fill ONLY these empty fields of the draft finding (leave the rest alone; state "
                "nothing the draft or research does not support; CVE ids only when named there; "
                "references only from the research). Fields to return: "
                if fill
                else "Rewrite the following draft finding. Fields to return: "
            )
            + ", ".join([*targets, *(["custom_fields"] if custom else [])]),
            "DRAFT FINDING (JSON):\n" + json.dumps(draft, indent=2, ensure_ascii=False),
        ]
        if custom:
            parts.append(
                "CUSTOM FIELDS TO FILL (JSON):\n" + json.dumps(custom, indent=2, ensure_ascii=False)
            )
        for tag, text in zip(("research", "library"), references, strict=True):
            if text:
                parts.append(f"<{tag}>\n{_neutralise(self._mask(text), tag)}\n</{tag}>")
        return "\n\n".join(parts)

    def _build_changes(
        self,
        finding: dict[str, Any],
        args: dict[str, Any],
        targets: tuple[str, ...],
        custom: list[dict[str, Any]],
        only_empty: bool = False,
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
            cvss, vector_change = self._resolve_cvss(finding, args, only_empty)
            if vector_change is not None and cvss is not None:
                payload["cvssv3"] = cvss.vector
                changes["cvssv3"] = vector_change
            merged_cf, cf_changes = _merge_custom_fields(
                finding, args.get("custom_fields"), labels, cvss
            )
            if cf_changes:
                payload["customFields"] = merged_cf
                changes.update(cf_changes)
        return payload, changes

    @staticmethod
    def _resolve_cvss(
        finding: dict[str, Any], args: dict[str, Any], only_empty: bool
    ) -> tuple[Cvss | None, dict[str, str] | None]:
        """The vector that score/severity fields are computed from, and the change it implies.

        When only filling gaps, a vector already on the finding is authoritative and never
        replaced. Without a valid vector from the model the finding's own vector is used. The
        change is ``{"before", "after"}`` when the vector differs from the finding's, else None.
        """
        current = str(finding.get("cvssv3") or "").strip()
        existing = parse_vector(current)
        if only_empty and current:
            return existing, None
        proposed = parse_vector(args.get("cvss_vector"))
        if proposed is None:
            return existing, None
        before = str(finding.get("cvssv3") or "")
        change = {"before": before, "after": proposed.vector} if proposed.vector != before else None
        return proposed, change

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
