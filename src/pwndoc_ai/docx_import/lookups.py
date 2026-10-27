"""Read-only PwnDoc lookups for the importer: preview dropdowns, defaults and name checks.

Everything PwnDoc returns is untrusted: an entry whose value does not pass the model's own
patterns and length caps is skipped, never passed on. Lists are capped at ``MAX_OPTIONS``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from ..pwndoc_client import PwnDocClient, PwnDocError
from . import labels, limits
from .records import AuditTypeOption, Choice, NameCheck, PlanDefaults, PwnDocOptions

log = logging.getLogger("pwndoc_ai.docx_import")

AUDIT_STAGES = ("", "default")  # "retest" and "multi" types cannot hold a plain audit
TEMPLATE_EXTS = ("", "docx")


def _plain(value: Any, limit: int, pattern: re.Pattern | None = None) -> str:
    """``value`` when it is a non-empty one-line string within ``limit``, else ``""``."""
    if not isinstance(value, str) or not value or len(value) > limit:
        return ""
    if limits.CONTROL_LINE.search(value):
        return ""
    if pattern is not None and not pattern.fullmatch(value):
        return ""
    return value


def ref_id(value: Any) -> str:
    """A PwnDoc id given as a string or as a populated ``{_id: ...}`` object, else ``""``."""
    if isinstance(value, dict):
        value = value.get("_id")
    return _plain(value, 24, limits.OBJECT_ID)


def _dicts(values: Any) -> list[dict[str, Any]]:
    return [v for v in values if isinstance(v, dict)] if isinstance(values, list) else []


def _unique(choices: Iterable[Any], key: str) -> tuple[Any, ...]:
    seen: set[str] = set()
    out = []
    for choice in choices:
        value = getattr(choice, key)
        if value not in seen and len(out) < limits.MAX_OPTIONS:
            seen.add(value)
            out.append(choice)
    return tuple(out)


def _languages(raw: Any) -> tuple[Choice, ...]:
    found = []
    for item in _dicts(raw):
        locale = _plain(item.get("locale"), 32, limits.LOCALE)
        label = _plain(item.get("language"), limits.MAX_NAME_CHARS) or locale
        if locale:
            found.append(Choice(value=locale, label=label))
    return _unique(found, "value")


def _type_locales(item: dict[str, Any]) -> dict[str, str]:
    """``{locale: template id}`` from an audit type's ``templates`` list."""
    out: dict[str, str] = {}
    for entry in _dicts(item.get("templates"))[:50]:
        locale = _plain(entry.get("locale"), 32, limits.LOCALE)
        template = ref_id(entry.get("template"))
        if locale and template and locale not in out:
            out[locale] = template
    return out


def _audit_types(raw: Any) -> tuple[tuple[AuditTypeOption, dict[str, str]], ...]:
    found: dict[str, tuple[AuditTypeOption, dict[str, str]]] = {}
    for item in _dicts(raw):
        name = _plain(item.get("name"), limits.MAX_NAME_CHARS)
        stage = item.get("stage") or ""
        if name and stage in AUDIT_STAGES and name not in found:
            locales = _type_locales(item)
            found[name] = (AuditTypeOption(name=name, locales=tuple(locales)), locales)
    return tuple(found.values())[: limits.MAX_OPTIONS]


def _templates(raw: Any) -> tuple[Choice, ...]:
    found = []
    for item in _dicts(raw):
        template_id = ref_id(item.get("_id"))
        name = _plain(item.get("name"), limits.MAX_NAME_CHARS) or template_id
        if template_id and (item.get("ext") or "") in TEMPLATE_EXTS:
            found.append(Choice(value=template_id, label=name))
    return _unique(found, "value")


def _newest_audit(client: PwnDocClient) -> dict[str, Any] | None:
    audits = [a for a in _dicts(client.list_audits()) if ref_id(a.get("_id"))]
    if not audits:
        return None
    return max(audits, key=lambda a: str(a.get("createdAt") or ""))


def _recent_template(client: PwnDocClient, audit_id: str) -> str:
    try:
        return ref_id(client.get_audit_general(audit_id).get("template"))
    except (PwnDocError, ValueError) as exc:
        log.info("import lookups: recent audit unreadable (%s)", type(exc).__name__)
        return ""


def _defaults(
    client: PwnDocClient,
    languages: tuple[Choice, ...],
    types: tuple[tuple[AuditTypeOption, dict[str, str]], ...],
    templates: tuple[Choice, ...],
) -> PlanDefaults:
    known_langs = {c.value for c in languages}
    type_map = {option.name: locales for option, locales in types}
    known_templates = {c.value for c in templates}
    language = languages[0].value if languages else ""
    audit_type = types[0][0].name if types else ""
    template = ""
    newest = _newest_audit(client)
    if newest is not None:
        if newest.get("language") in known_langs:
            language = newest["language"]
        if newest.get("auditType") in type_map:
            audit_type = newest["auditType"]
        template = _recent_template(client, ref_id(newest.get("_id")))
    if template not in known_templates:
        template = type_map.get(audit_type, {}).get(language, "")
    if template not in known_templates:
        template = templates[0].value if templates else ""
    return PlanDefaults(language=language, audit_type=audit_type, template_id=template)


def load_options(client: PwnDocClient) -> tuple[PwnDocOptions, PlanDefaults]:
    """Languages, audit types (with their template locales) and templates, plus defaults taken
    from the user's newest audit (else the first options). The default template is listed first."""
    languages = _languages(client.list_languages())
    types = _audit_types(client.list_audit_types())
    templates = _templates(client.list_templates())
    defaults = _defaults(client, languages, types, templates)
    ordered = tuple(
        sorted(templates, key=lambda c: c.value != defaults.template_id)
    )  # stable: the default first, the rest in PwnDoc's order
    options = PwnDocOptions(
        languages=languages,
        audit_types=tuple(option for option, _ in types),
        templates=ordered,
    )
    return options, defaults


def find_company(companies: Any, name: str) -> str:
    """The id of the company whose name matches ``name`` case-insensitively, else ``""``."""
    wanted = labels.name_key(name)
    if not wanted:
        return ""
    for company in _dicts(companies):
        found = company.get("name")
        if isinstance(found, str) and labels.name_key(found) == wanted:
            company_id = ref_id(company.get("_id"))
            if company_id:
                return company_id
    return ""


def is_duplicate_audit(audits: Any, audit_name: str, company_id: str) -> bool:
    """True when a visible audit has the same name (case-insensitive) and the same company
    (or, for a company that does not exist yet, no company at all)."""
    wanted = labels.name_key(audit_name)
    if not wanted:
        return False
    for audit in _dicts(audits):
        name = audit.get("name")
        if not isinstance(name, str) or labels.name_key(name) != wanted:
            continue
        if ref_id(audit.get("company")) == company_id:
            return True
    return False


def check_names(client: PwnDocClient, company: str, audit_name: str) -> NameCheck:
    company_id = find_company(client.list_companies(), company)
    duplicate = is_duplicate_audit(client.list_audits(), audit_name, company_id)
    return NameCheck(company_id=company_id, duplicate_audit=duplicate)
