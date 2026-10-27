"""Data model of the typography engine: the font plan, its precedence rules and the result.

Everything here is pure data (no lxml). ``from_json`` treats its input as hostile (a browser body
or a file on disk): only the documented keys, strict types, closed enums, sizes in 0.5 pt steps,
family names through ``font_catalog_model.clean_family``. ``to_json`` output is accepted again
unchanged by ``from_json`` (round trip is the identity).

Precedence (docs/EXPORT_DESIGN.md (i)), per property (``family`` and ``size`` independently),
first value that is set wins:

1. ``by_section[s].by_block_type[t]``
2. ``by_section[s].all`` (only for the types that inherit a default, see ``block_types``)
3. ``by_block_type[t]``
4. ``default`` (same inheritance rule)
5. nothing: the document keeps its own value.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .. import export_json as jc
from ..block_types import BLOCK_TYPES, DEFAULT_FAMILY_TYPES, DEFAULT_SIZE_TYPES, HEADING_TYPES
from ..export_errors import ExportInputError
from ..font_catalog_model import clean_family, half_points, parse_size

PLAN_VERSION = 1
MAX_SECTIONS_IN_PLAN = 2000
MAX_REVISION = 1_000_000
SECTION_ID = re.compile(r"s[1-9][0-9]{0,4}")
BLOCK_ID = re.compile(r"b[0-9]{5}")
BLOCK_TYPE = re.compile("|".join(BLOCK_TYPES))
MAX_FONT_USES = 20


@dataclass(frozen=True)
class FontSpec:
    """A family and/or a size. ``family == ""`` and ``size is None`` mean "not set here"."""

    family: str = ""
    size: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "family", clean_family(self.family, "family"))
        if self.size is not None:
            object.__setattr__(self, "size", parse_size(self.size, "size"))

    @property
    def is_empty(self) -> bool:
        return not self.family and self.size is None

    @property
    def half_points(self) -> int | None:
        return None if self.size is None else half_points(self.size)

    def to_json(self) -> dict[str, Any]:
        return {"family": self.family or None, "size": self.size}

    @classmethod
    def from_json(cls, value: Any, path: str = "spec") -> FontSpec:
        data = jc.record(value, path, (), ("family", "size"))
        family = data.get("family")
        size = data.get("size")
        return cls(
            family="" if family is None else clean_family(family, f"{path}.family"),
            size=None if size is None else parse_size(size, f"{path}.size"),
        )


EMPTY = FontSpec()


def _type_map(value: Any, path: str) -> tuple[tuple[str, FontSpec], ...]:
    data = jc.mapping(value, path, len(BLOCK_TYPES), BLOCK_TYPE)
    pairs = [
        (name, FontSpec.from_json(data[name], f"{path}.{name}"))
        for name in BLOCK_TYPES
        if name in data
    ]
    return tuple((name, spec) for name, spec in pairs if not spec.is_empty)


def _canonical_types(pairs: Iterable[tuple[str, FontSpec]]) -> tuple[tuple[str, FontSpec], ...]:
    given = dict(pairs)
    unknown = set(given) - set(BLOCK_TYPES)
    if unknown:
        raise ExportInputError("invalid", field="by_block_type")
    return tuple((name, given[name]) for name in BLOCK_TYPES if not given.get(name, EMPTY).is_empty)


@dataclass(frozen=True)
class SectionStyle:
    """Overrides for one section: ``all`` (a section-wide default) and per block type."""

    all: FontSpec = EMPTY
    by_block_type: tuple[tuple[str, FontSpec], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_block_type", _canonical_types(self.by_block_type))

    @property
    def is_empty(self) -> bool:
        return self.all.is_empty and not self.by_block_type

    def for_type(self, block_type: str) -> FontSpec:
        return dict(self.by_block_type).get(block_type, EMPTY)

    def to_json(self) -> dict[str, Any]:
        return {
            "all": self.all.to_json(),
            "by_block_type": {name: spec.to_json() for name, spec in self.by_block_type},
        }

    @classmethod
    def from_json(cls, value: Any, path: str = "section") -> SectionStyle:
        data = jc.record(value, path, (), ("all", "by_block_type"))
        return cls(
            all=FontSpec.from_json(data.get("all", {}), f"{path}.all"),
            by_block_type=_type_map(data.get("by_block_type", {}), f"{path}.by_block_type"),
        )


def _inherits(spec: FontSpec, block_type: str) -> FontSpec:
    """The part of a default ``spec`` that ``block_type`` inherits."""
    return FontSpec(
        family=spec.family if block_type in DEFAULT_FAMILY_TYPES else "",
        size=spec.size if block_type in DEFAULT_SIZE_TYPES else None,
    )


def _first(*specs: FontSpec) -> FontSpec:
    family = next((spec.family for spec in specs if spec.family), "")
    size = next((spec.size for spec in specs if spec.size is not None), None)
    return FontSpec(family=family, size=size)


@dataclass(frozen=True)
class TypographyPlan:
    """The whole plan. Entries that are empty are dropped, so equal plans compare equal."""

    default: FontSpec = EMPTY
    by_block_type: tuple[tuple[str, FontSpec], ...] = ()
    by_section: tuple[tuple[str, SectionStyle], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_block_type", _canonical_types(self.by_block_type))
        sections = tuple((sid, style) for sid, style in self.by_section if not style.is_empty)
        if len(sections) > MAX_SECTIONS_IN_PLAN:
            raise ExportInputError("invalid", field="by_section")
        if any(not SECTION_ID.fullmatch(sid) for sid, _ in sections):
            raise ExportInputError("invalid", field="by_section")
        jc.unique((sid for sid, _ in sections), "by_section")
        ordered = tuple(sorted(sections, key=lambda item: int(item[0][1:])))
        object.__setattr__(self, "by_section", ordered)

    @property
    def is_empty(self) -> bool:
        return self.default.is_empty and not self.by_block_type and not self.by_section

    def type_spec(self, block_type: str) -> FontSpec:
        """The explicit ``by_block_type`` entry (no inheritance)."""
        return dict(self.by_block_type).get(block_type, EMPTY)

    def section(self, section_id: str) -> SectionStyle:
        return dict(self.by_section).get(section_id, SectionStyle())

    def resolve(self, block_type: str, section_id: str | None = None) -> FontSpec:
        """The effective spec for a block of ``block_type`` in ``section_id`` (see the module
        docstring). With ``section_id=None`` this is the style-level spec of that type."""
        if block_type not in BLOCK_TYPES:
            raise ExportInputError("invalid", field="block_type")
        style = self.section(section_id) if section_id else SectionStyle()
        return _first(
            style.for_type(block_type),
            _inherits(style.all, block_type),
            self.type_spec(block_type),
            _inherits(self.default, block_type),
        )

    def families(self) -> tuple[str, ...]:
        """Every family the plan names, in first-seen order (for the font-status notice)."""
        specs = [self.default, *(spec for _, spec in self.by_block_type)]
        for _, style in self.by_section:
            specs.extend([style.all, *(spec for _, spec in style.by_block_type)])
        return tuple(dict.fromkeys(spec.family for spec in specs if spec.family))

    def check_sections(self, known: Iterable[str]) -> None:
        """Refuse a plan that names a section the document does not have."""
        allowed = set(known)
        for sid, _ in self.by_section:
            if sid not in allowed:
                raise ExportInputError("unknown_section", field=f"plan.by_section.{sid}")

    def digest(self) -> str:
        """16 hex characters identifying the plan (cache keys: ``typ:<digest>``)."""
        raw = json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_json(self) -> dict[str, Any]:
        return {
            "version": PLAN_VERSION,
            "default": self.default.to_json(),
            "by_block_type": {name: spec.to_json() for name, spec in self.by_block_type},
            "by_section": {sid: style.to_json() for sid, style in self.by_section},
        }

    @classmethod
    def from_json(cls, value: Any, path: str = "plan") -> TypographyPlan:
        data = jc.record(value, path, ("version",), ("default", "by_block_type", "by_section"))
        if data["version"] != PLAN_VERSION or isinstance(data["version"], bool):
            raise jc.fail(f"{path}.version")
        sections = jc.mapping(
            data.get("by_section", {}), f"{path}.by_section", MAX_SECTIONS_IN_PLAN, SECTION_ID
        )
        return cls(
            default=FontSpec.from_json(data.get("default", {}), f"{path}.default"),
            by_block_type=_type_map(data.get("by_block_type", {}), f"{path}.by_block_type"),
            by_section=tuple(
                (sid, SectionStyle.from_json(item, f"{path}.by_section.{sid}"))
                for sid, item in sections.items()
            ),
        )


def _spec(family: str, size: float) -> FontSpec:
    return FontSpec(family=family, size=size if size else None)


def plan_from_defaults(
    *,
    default_font: str = "",
    default_size: float = 0.0,
    heading_font: str = "",
    heading_size: float = 0.0,
    mono_font: str = "",
    mono_size: float = 0.0,
) -> TypographyPlan:
    """The plan for the admin defaults in Settings (already normalised: sizes are 0 or a valid
    size). ``heading_*`` applies to ``title`` and ``heading_1``..``heading_4``; ``mono_*`` to
    ``code``. Empty values give an empty plan (``is_empty``): the download is left unchanged."""
    heading = _spec(heading_font, heading_size)
    pairs = [(name, heading) for name in HEADING_TYPES]
    pairs.append(("code", _spec(mono_font, mono_size)))
    return TypographyPlan(default=_spec(default_font, default_size), by_block_type=tuple(pairs))


@dataclass(frozen=True)
class BlockAssignment:
    """Where a body block sits (from section detection): its section and its block type."""

    section_id: str
    block_type: str

    def __post_init__(self) -> None:
        if not SECTION_ID.fullmatch(self.section_id) or self.block_type not in BLOCK_TYPES:
            raise ExportInputError("invalid", field="assignment")


@dataclass(frozen=True)
class FontUse:
    """A family seen in the document and how many runs (or styles) request it."""

    family: str
    runs: int

    def to_json(self) -> dict[str, Any]:
        return {"family": self.family, "runs": self.runs}

    @classmethod
    def from_json(cls, value: Any, path: str = "font") -> FontUse:
        data = jc.record(value, path, ("family", "runs"))
        family = clean_family(data["family"], f"{path}.family")
        if not family:
            raise jc.fail(f"{path}.family")
        return cls(family=family, runs=jc.count(data["runs"], f"{path}.runs"))


def font_uses_from_json(value: Any, path: str = "fonts") -> tuple[FontUse, ...]:
    return jc.items(value, path, MAX_FONT_USES, FontUse.from_json)


@dataclass(frozen=True)
class TypographyResult:
    """What ``apply_typography`` produced. ``data`` is the new DOCX (identical bytes to the input
    when nothing had to change). Counters are for logs and the ``X-Typography-*`` headers."""

    data: bytes
    runs_changed: int = 0
    styles_changed: int = 0
    theme_changed: bool = False
    symbol_runs_kept: int = 0
    families: tuple[str, ...] = field(default_factory=tuple)

    @property
    def changed(self) -> bool:
        return bool(self.runs_changed or self.styles_changed or self.theme_changed)

    def headers(self) -> dict[str, str]:
        """Digits only, safe for HTTP headers."""
        return {
            "X-Typography": "1" if self.changed else "0",
            "X-Typography-Runs": str(self.runs_changed),
            "X-Typography-Styles": str(self.styles_changed),
        }


def check_assignments(assignments: Mapping[str, BlockAssignment]) -> dict[str, BlockAssignment]:
    """Validate an assignment map handed to the engine (block ids and values)."""
    if any(not BLOCK_ID.fullmatch(key) for key in assignments):
        raise ExportInputError("invalid", field="assignments")
    if any(not isinstance(item, BlockAssignment) for item in assignments.values()):
        raise ExportInputError("invalid", field="assignments")
    return dict(assignments)


__all__ = [
    "BLOCK_ID",
    "EMPTY",
    "MAX_REVISION",
    "MAX_SECTIONS_IN_PLAN",
    "PLAN_VERSION",
    "SECTION_ID",
    "BlockAssignment",
    "FontSpec",
    "FontUse",
    "SectionStyle",
    "TypographyPlan",
    "TypographyResult",
    "check_assignments",
    "font_uses_from_json",
    "plan_from_defaults",
]
