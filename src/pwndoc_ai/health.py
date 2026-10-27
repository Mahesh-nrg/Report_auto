"""Setup health check and one-click first-run fixes for a PwnDoc instance.

``run_checks`` inspects what the AI features rely on (languages, audit types, templates, custom
fields, LLM, RAG index, PDF converter) and never raises: a PwnDoc failure becomes an ``error``
check.  ``apply_fixes`` repairs the safe subset idempotently and returns what it changed.
"""

from __future__ import annotations

import copy
import ipaddress
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, NoReturn
from urllib.parse import urlsplit

from .config import Settings
from .font_health import check_font_catalog
from .font_probe import resolve_family
from .jsonio import write_json_atomic
from .pwndoc_client import PwnDocClient, PwnDocError

log = logging.getLogger(__name__)

OK, WARN, ERROR = "ok", "warn", "error"
DETAIL_LIMIT = 200
DEFAULT_LANGUAGE = ("English", "en")
PREFERRED_LOCALE = "en"
FIELD_DISPLAY = "finding"
FIELD_TYPE = "input"
FIELD_DESCRIPTION = "Filled by the AI report assistant."
VERIFY_ATTEMPTS = 6  # PwnDoc answers before its inserts finish; give them a moment
VERIFY_DELAY = 0.5
SETUP_BACKUP_DIR = Path("backups") / "_setup"
REWRITE_NOTE = " Fixing replaces the whole audit type list in PwnDoc (a backup is saved first)."
PDF_HINT = (
    "PDF export needs LibreOffice: sudo apt install libreoffice-writer-nogui "
    "(or set PWNDOC_AI_SOFFICE to its soffice path)"
)
FONT_PACKAGES = "fonts-crosextra-carlito fonts-crosextra-caladea"
FONT_HINT = f"sudo apt install {FONT_PACKAGES}"
# Word font -> the metric-compatible open font that has the same glyph widths.
FONT_SUBSTITUTES = (("Calibri", "Carlito"), ("Aptos", "Carlito"), ("Cambria", "Caladea"))


class HealthFixError(RuntimeError):
    """A repair could not be completed safely; the message is fit to show to an administrator."""


@dataclass(frozen=True)
class Check:
    id: str
    label: str
    status: str  # 'ok' | 'warn' | 'error'
    detail: str
    fixable: bool = False


@dataclass(frozen=True)
class _AiField:
    label: str  # label used when the field has to be created
    pattern: re.Pattern[str]  # any existing finding field whose label matches counts as present


AI_FIELDS: tuple[_AiField, ...] = (
    _AiField("CWE", re.compile(r"cwe|cve", re.IGNORECASE)),
    _AiField("CVSS Score", re.compile(r"cvss.*score|score.*cvss", re.IGNORECASE)),
    _AiField("Severity", re.compile(r"severity", re.IGNORECASE)),
    _AiField("Vulnerable URL", re.compile(r"url|location", re.IGNORECASE)),
)


class _Fetched(NamedTuple):
    data: list[dict[str, Any]] | None
    error: str | None


# -- fetching ---------------------------------------------------------------------------------
def _short(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text if len(text) <= DETAIL_LIMIT else text[: DETAIL_LIMIT - 3] + "..."


def _fetch(call: Callable[[], Any]) -> _Fetched:
    try:
        data = call()
    except PwnDocError as exc:
        return _Fetched(None, _short(exc))
    except Exception as exc:  # a check must never raise; the cause is logged and surfaced
        log.exception("health check fetch failed")
        return _Fetched(None, f"unexpected error: {_short(exc)}")
    rows = [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
    return _Fetched(rows, None)


def _guard(check_id: str, label: str, build: Callable[[], Check]) -> Check:
    try:
        return build()
    except Exception as exc:  # defensive: malformed data must not break the whole report
        log.exception("health check %s failed", check_id)
        return Check(check_id, label, ERROR, f"unexpected error: {_short(exc)}")


# -- data helpers -----------------------------------------------------------------------------
def _locales(languages: list[dict[str, Any]]) -> list[str]:
    return [str(lang["locale"]) for lang in languages if lang.get("locale")]


def _template_id(entry: dict[str, Any]) -> Any:
    template = entry.get("template")
    return template.get("_id") if isinstance(template, dict) else template


def _usable(audit_type: dict[str, Any]) -> dict[str, Any]:
    """locale -> template id for the template slots that really point at a template."""
    usable: dict[str, Any] = {}
    for entry in audit_type.get("templates") or []:
        if isinstance(entry, dict) and entry.get("locale") and _template_id(entry):
            usable.setdefault(str(entry["locale"]), _template_id(entry))
    return usable


def _missing_locales(audit_type: dict[str, Any], locales: list[str]) -> list[str]:
    have = _usable(audit_type)
    return [loc for loc in locales if loc not in have]


def _source_locale(usable: dict[str, Any]) -> str | None:
    if PREFERRED_LOCALE in usable:
        return PREFERRED_LOCALE
    return next(iter(usable), None)


def _missing_ai_fields(fields: list[dict[str, Any]]) -> list[_AiField]:
    finding_labels = [
        str(f.get("label") or "") for f in fields if f.get("display") == FIELD_DISPLAY
    ]
    return [
        spec for spec in AI_FIELDS if not any(spec.pattern.search(lbl) for lbl in finding_labels)
    ]


# -- checks -----------------------------------------------------------------------------------
def _error(check_id: str, label: str, message: str) -> Check:
    return Check(check_id, label, ERROR, message)


def _check_languages(fetched: _Fetched) -> Check:
    label = "Languages"
    if fetched.data is None:
        return _error("languages", label, fetched.error or "unavailable")
    if not fetched.data:
        return Check(
            "languages", label, ERROR, "No language defined in PwnDoc; reports need one.", True
        )
    names = ", ".join(f"{x.get('language', '?')} ({x.get('locale', '?')})" for x in fetched.data)
    return Check("languages", label, OK, names)


def _check_audit_types(fetched: _Fetched) -> Check:
    label = "Audit types"
    if fetched.data is None:
        return _error("audit_types", label, fetched.error or "unavailable")
    if not fetched.data:
        return _error("audit_types", label, "No audit type defined (Data > Audit Types).")
    return Check("audit_types", label, OK, ", ".join(str(t.get("name", "?")) for t in fetched.data))


def _check_templates(fetched: _Fetched) -> Check:
    label = "Report templates"
    if fetched.data is None:
        return _error("templates", label, fetched.error or "unavailable")
    if not fetched.data:
        return _error("templates", label, "No template uploaded (Data > Templates, .docx).")
    return Check("templates", label, OK, f"{len(fetched.data)} template(s) uploaded")


def _check_coverage(languages: _Fetched, types: _Fetched) -> Check:
    label = "Template coverage"
    for source in (languages, types):
        if source.data is None:
            return _error("template_coverage", label, f"not checked: {source.error}")
    if not languages.data or not types.data:
        return Check("template_coverage", label, WARN, "Needs a language and an audit type first.")
    locales = _locales(languages.data)
    gaps = [(t, _missing_locales(t, locales)) for t in types.data]
    gaps = [(t, missing) for t, missing in gaps if missing]
    if not gaps:
        return Check("template_coverage", label, OK, "Every audit type has a template per language")
    detail = "; ".join(f"{t.get('name', '?')}: no template for {', '.join(m)}" for t, m in gaps)
    fixable = any(_usable(t) for t, _ in gaps)
    return Check(
        "template_coverage", label, WARN, detail + (REWRITE_NOTE if fixable else ""), fixable
    )


def _check_custom_fields(fetched: _Fetched) -> Check:
    label = "AI custom fields"
    if fetched.data is None:
        return _error("custom_fields", label, fetched.error or "unavailable")
    missing = _missing_ai_fields(fetched.data)
    if not missing:
        return Check("custom_fields", label, OK, "CWE, CVSS score, severity and URL fields exist")
    names = ", ".join(spec.label for spec in missing)
    return Check("custom_fields", label, WARN, f"Missing finding fields: {names}", True)


def _check_llm(settings: Settings) -> Check:
    chain = " -> ".join(settings.model_chain) or "none"
    if settings.llm_configured:
        return Check("llm", "AI model", OK, f"model chain: {chain}")
    return Check(
        "llm", "AI model", WARN, f"LLM not configured (needs API key); model chain: {chain}"
    )


def _check_rag(documents: int | None) -> Check:
    label = "Knowledge index (RAG)"
    if documents is None:
        return Check("rag", label, WARN, "Index unavailable; local knowledge is not used.")
    if documents == 0:
        return Check("rag", label, WARN, "No documents indexed; add some to the knowledge folder.")
    return Check("rag", label, OK, f"{documents} document(s) indexed")


def _check_pdf(soffice: str | None) -> Check:
    if soffice:
        return Check("pdf", "PDF export", OK, f"LibreOffice found: {soffice}")
    return Check("pdf", "PDF export", WARN, PDF_HINT)


def _same_family(found: str | None, wanted: str) -> bool:
    return found is not None and found.casefold() == wanted.casefold()


def _safe_resolve(resolver: Callable[[str], str | None], family: str) -> str | None:
    try:
        return resolver(family)
    except Exception:  # noqa: BLE001 - a broken probe is "unknown", never a failed check
        log.warning("font lookup failed")
        return None


def _substituted(resolver: Callable[[str], str | None]) -> tuple[list[str], bool]:
    """(families a wider fallback font would replace, whether fontconfig answered at all)."""
    lookups = {name: _safe_resolve(resolver, name) for pair in FONT_SUBSTITUTES for name in pair}
    if all(found is None for found in lookups.values()):
        return [], False
    bad = [
        word
        for word, metric in FONT_SUBSTITUTES
        if not (_same_family(lookups[word], word) or _same_family(lookups[word], metric))
        and not _same_family(lookups[metric], metric)
    ]
    return bad, True


def _check_pdf_fonts(resolver: Callable[[str], str | None]) -> Check:
    label = "PDF fonts"
    bad, answered = _substituted(resolver)
    if not answered:
        return Check("PDF_FONTS", label, OK, "Fonts were not checked: fontconfig is not available")
    if not bad:
        return Check("PDF_FONTS", label, OK, "Calibri, Aptos and Cambria have matching fonts")
    names = ", ".join(bad)
    return Check(
        "PDF_FONTS",
        label,
        WARN,
        f"{names} would be replaced by a wider font in the PDF, which moves line and page "
        f"breaks. Install the metric-compatible fonts: {FONT_HINT}",
    )


def _check_web_search(settings: Settings) -> Check:
    state = (
        "on (finding titles are sent to the search engine)" if settings.use_web_search else "off"
    )
    return Check("web_search", "Web search", OK, f"Web research is {state}")


def _is_private_host(host: str) -> bool:
    if host in {"localhost", ""} or "." not in host and ":" not in host:
        return True  # a bare Docker/intranet name such as "languagetool"
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return host.endswith((".local", ".internal", ".lan"))
    return address.is_private or address.is_loopback


def _check_spelling(settings: Settings) -> Check:
    label = "Spelling check (LanguageTool)"
    url = settings.languagetool_url
    if not url:
        return Check("spelling", label, OK, "Off (no LanguageTool URL)")
    host = urlsplit(url).hostname or ""
    if _is_private_host(host):
        return Check("spelling", label, OK, f"Finding text is sent to {host} (not masked)")
    return Check(
        "spelling",
        label,
        WARN,
        f"Finding text is sent UNMASKED to {host}, which looks like a public server; "
        "use a LanguageTool server you host yourself",
    )


def run_checks(
    client: PwnDocClient,
    settings: Settings,
    *,
    rag_documents: int | None,
    soffice: str | None,
    font_resolver: Callable[[str], str | None] = resolve_family,
) -> list[Check]:
    """Every setup check, in display order. Never raises."""
    languages = _fetch(client.list_languages)
    types = _fetch(client.list_audit_types)
    templates = _fetch(client.list_templates)
    fields = _fetch(client.list_custom_fields)
    builders: tuple[tuple[str, str, Callable[[], Check]], ...] = (
        ("languages", "Languages", lambda: _check_languages(languages)),
        ("audit_types", "Audit types", lambda: _check_audit_types(types)),
        ("templates", "Report templates", lambda: _check_templates(templates)),
        ("template_coverage", "Template coverage", lambda: _check_coverage(languages, types)),
        ("custom_fields", "AI custom fields", lambda: _check_custom_fields(fields)),
        ("llm", "AI model", lambda: _check_llm(settings)),
        ("rag", "Knowledge index (RAG)", lambda: _check_rag(rag_documents)),
        ("pdf", "PDF export", lambda: _check_pdf(soffice)),
        ("PDF_FONTS", "PDF fonts", lambda: _check_pdf_fonts(font_resolver)),
        ("FONTS_CATALOG", "Font catalog", lambda: check_font_catalog(None, font_resolver)),
        ("web_search", "Web search", lambda: _check_web_search(settings)),
        ("spelling", "Spelling check (LanguageTool)", lambda: _check_spelling(settings)),
    )
    return [_guard(check_id, label, build) for check_id, label, build in builders]


# -- fixes ------------------------------------------------------------------------------------
def _ensure_language(client: PwnDocClient) -> tuple[list[str], list[str]]:
    languages = [x for x in client.list_languages() if isinstance(x, dict)]
    if languages:
        return _locales(languages), []
    language, locale = DEFAULT_LANGUAGE
    client.create_language(language, locale)
    return [locale], [f"Created the {language} ({locale}) language"]


def _audit_type_payload(audit_type: dict[str, Any], templates: list[Any]) -> dict[str, Any]:
    return {
        "name": audit_type.get("name"),
        "templates": templates,
        "sections": copy.deepcopy(audit_type.get("sections") or []),
        "hidden": copy.deepcopy(audit_type.get("hidden") or []),
        "stage": audit_type.get("stage") or "default",
    }


def _filled_templates(
    audit_type: dict[str, Any], locales: list[str]
) -> tuple[list[Any], str, list[str]] | None:
    """New template list for one audit type, or None when nothing can/needs to change."""
    usable = _usable(audit_type)
    missing = _missing_locales(audit_type, locales)
    source = _source_locale(usable)
    if not missing or source is None:
        return None
    keep = [
        copy.deepcopy(e)
        for e in audit_type.get("templates") or []
        if not (isinstance(e, dict) and e.get("locale") in missing)
    ]
    added = [{"template": usable[source], "locale": loc} for loc in missing]
    return keep + added, source, missing


def _signature(audit_types: list[dict[str, Any]]) -> dict[str, frozenset[str]]:
    """Audit type name -> locales that have a usable template (what the fix must preserve)."""
    return {str(t.get("name")): frozenset(_usable(t)) for t in audit_types if isinstance(t, dict)}


def _save_snapshot(backup_dir: Path | None, audit_types: list[dict[str, Any]]) -> Path | None:
    """Write the audit types to ``backup_dir``; the file's path (None when no folder is set)."""
    if backup_dir is None:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = backup_dir / f"audit-types-{stamp}.json"
    try:
        write_json_atomic(path, audit_types)
    except OSError as exc:
        raise HealthFixError(
            "could not save a backup of the audit types, so nothing was changed"
        ) from exc
    return path


def _payload_of(audit_types: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_audit_type_payload(t, copy.deepcopy(t.get("templates") or [])) for t in audit_types]


def _current_types(client: PwnDocClient) -> list[dict[str, Any]] | None:
    """The audit types now, or None when PwnDoc cannot be read (logged, never raised)."""
    try:
        return [t for t in client.list_audit_types() if isinstance(t, dict)]
    except Exception:  # noqa: BLE001 - recovery must go on when the read fails too
        log.exception("could not re-read the audit types while verifying")
        return None


def _wait_for(
    client: PwnDocClient, expected: dict[str, frozenset[str]]
) -> list[dict[str, Any]] | None:
    """Poll until the audit types match ``expected`` (PwnDoc inserts after it has answered).

    Returns the matching list, or None after ``VERIFY_ATTEMPTS`` reads without a match.
    """
    for attempt in range(VERIFY_ATTEMPTS):
        if attempt:
            time.sleep(VERIFY_DELAY)
        after = _current_types(client)
        if after is not None and _signature(after) == expected:
            return after
    return None


def _put_back(client: PwnDocClient, snapshot: list[dict[str, Any]]) -> bool:
    """Replace the audit types with ``snapshot`` and prove it took; False when it did not."""
    try:
        client.update_audit_types(_payload_of(snapshot))
    except Exception:  # noqa: BLE001 - reported to the admin with the snapshot location
        log.exception("restoring the previous audit types failed")
        return False
    return _wait_for(client, _signature(snapshot)) is not None


def _where(snapshot_path: Path | None) -> str:
    return f"a copy is saved at {snapshot_path}" if snapshot_path else "no copy was saved"


def _roll_back_and_raise(
    client: PwnDocClient,
    snapshot: list[dict[str, Any]],
    snapshot_path: Path | None,
    cause: BaseException | None,
) -> NoReturn:
    """Put the previous list back (verified) and raise what happened, naming the snapshot."""
    log.error("audit types did not match after the update; restoring the previous list")
    current = _current_types(client)
    if current is not None and current == snapshot:
        raise HealthFixError(
            "PwnDoc did not apply the audit type update; nothing was changed "
            f"({_where(snapshot_path)})"
        ) from cause
    if _put_back(client, snapshot):
        raise HealthFixError(
            "PwnDoc did not keep the updated audit types, so the previous list was put back; "
            f"check Data > Audit Types ({_where(snapshot_path)})"
        ) from cause
    raise HealthFixError(
        "PwnDoc lost the audit types and the previous list could not be verified as restored; "
        f"restore it in Data > Audit Types from the saved copy ({_where(snapshot_path)})"
    ) from cause


def _verify_or_roll_back(
    client: PwnDocClient,
    snapshot: list[dict[str, Any]],
    expected: dict[str, frozenset[str]],
    snapshot_path: Path | None = None,
) -> None:
    """PUT /audit-types deletes everything, then inserts without waiting: prove it worked."""
    if _wait_for(client, expected) is None:
        _roll_back_and_raise(client, snapshot, snapshot_path, None)


def _recover_failed_put(
    client: PwnDocClient,
    snapshot: list[dict[str, Any]],
    expected: dict[str, frozenset[str]],
    snapshot_path: Path | None,
    cause: Exception,
) -> NoReturn:
    """The PUT raised, but PwnDoc may already have deleted (or applied) everything: check."""
    log.error("audit type update failed (%s); verifying", cause.__class__.__name__)
    if _wait_for(client, expected) is not None:
        raise HealthFixError(
            "the update request failed but PwnDoc applied the new audit types (verified); "
            f"check Data > Audit Types and run the fix again ({_where(snapshot_path)})"
        ) from cause
    _roll_back_and_raise(client, snapshot, snapshot_path, cause)


def _plan_templates(
    audit_types: list[dict[str, Any]], locales: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    payload: list[dict[str, Any]] = []
    changes: list[str] = []
    for audit_type in audit_types:
        filled = _filled_templates(audit_type, locales)
        templates = audit_type.get("templates") or []
        if filled is not None:
            templates, source, missing = filled
            changes.append(
                f"Audit type '{audit_type.get('name')}': copied the '{source}' template "
                f"to {', '.join(missing)}"
            )
        payload.append(_audit_type_payload(audit_type, copy.deepcopy(templates)))
    return payload, changes


def _fill_templates(
    client: PwnDocClient, locales: list[str], backup_dir: Path | None = None
) -> list[str]:
    audit_types = [t for t in client.list_audit_types() if isinstance(t, dict)]
    payload, changes = _plan_templates(audit_types, locales)
    if not changes:
        return changes
    snapshot = copy.deepcopy(audit_types)
    snapshot_path = _save_snapshot(backup_dir, snapshot)
    if [t for t in client.list_audit_types() if isinstance(t, dict)] != snapshot:
        raise HealthFixError(
            "the audit types were changed in PwnDoc while the fix was being prepared; "
            "nothing was written, run the fix again"
        )
    try:
        client.update_audit_types(payload)
    except Exception as exc:
        _recover_failed_put(client, snapshot, _signature(payload), snapshot_path, exc)
    _verify_or_roll_back(client, snapshot, _signature(payload), snapshot_path)
    return changes


def _field_payload(spec: _AiField, locales: list[str]) -> dict[str, Any]:
    return {
        "fieldType": FIELD_TYPE,
        "label": spec.label,
        "display": FIELD_DISPLAY,
        "description": FIELD_DESCRIPTION,
        "text": [{"locale": loc, "value": ""} for loc in locales],
    }


def _create_fields(client: PwnDocClient, locales: list[str]) -> list[str]:
    existing = [f for f in client.list_custom_fields() if isinstance(f, dict)]
    changes: list[str] = []
    for spec in _missing_ai_fields(existing):
        client.create_custom_field(_field_payload(spec, locales))
        changes.append(f"Created the '{spec.label}' custom field on findings")
    return changes


def apply_fixes(client: PwnDocClient, data_dir: str | Path | None = None) -> list[str]:
    """Repair the safe first-run gaps; returns what changed (empty when nothing to do).

    Idempotent. PwnDoc errors propagate to the caller (nothing is swallowed). Because PwnDoc can
    only replace ALL audit types at once, the current list is saved under
    ``<data_dir>/backups/_setup/`` first, re-read afterwards and put back if it does not match
    (:class:`HealthFixError`).
    """
    backup_dir = Path(data_dir) / SETUP_BACKUP_DIR if data_dir is not None else None
    locales, changes = _ensure_language(client)
    changes = [
        *changes,
        *_fill_templates(client, locales, backup_dir),
        *_create_fields(client, locales),
    ]
    return changes
