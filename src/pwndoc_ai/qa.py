"""Report QA pass: a pure lint over a PwnDoc audit, plus an optional LanguageTool spelling check.

``lint_audit`` never touches the network and never raises: the audit is whatever
``GET /api/audits/{id}`` returned, which is treated as untrusted (missing keys, ``None``, wrong
types, huge strings). Messages are short and actionable and echo at most ``ECHO_LIMIT``
characters of user content. ``check_spelling`` talks to a self-hosted LanguageTool server, only
ever reports ``info`` issues and returns ``None`` (never ``[]``) when the service failed or its URL
was refused, so a caller can tell "no mistakes" from "no answer" and cache only the former.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from pwndoc_ai.cvss import Cvss, is_score_label, is_severity_label, parse_vector, severity_of
from pwndoc_ai.netguard import SafeTarget, inspect_http_url, pinned_requests

log = logging.getLogger(__name__)

ECHO_LIMIT = 60
MAX_TEXT_CHARS = 200_000
MAX_FINDINGS = 300
MAX_TITLE_CHARS = 80
MAX_DUPLICATE_TITLES = 100  # titles compared pairwise for near-duplicates
DUPLICATE_DEADLINE = 3.0  # seconds for the near-duplicate pass: /qa runs while a user waits
NEAR_DUPLICATE_RATIO = 0.9
SCORE_TOLERANCE = 0.05
MAX_CHECK_CHARS = 10_000
MAX_SPELLING_REQUESTS = 60
SPELLING_DEADLINE = 20.0  # seconds for the whole pass: it runs while a user waits
MAX_RESPONSE_BYTES = 1_000_000
SPELLING_FIELDS = ("title", "description", "observation", "remediation")
FILLABLE_TYPES = frozenset({"input", "text", "select"})
NA_VALUES = frozenset({"n/a", "na", "n.a", "n.a."})
NA_FIELDS = ("description", "observation", "remediation", "poc")
PLACEHOLDER_FIELDS = ("title", "description", "observation", "poc", "scope", "remediation")
SEVERITY_WORDS = {
    "critical": "Critical",
    "high": "High",
    "medium": "Medium",
    "moderate": "Medium",
    "low": "Low",
    "none": "None",
    "info": "None",
    "informational": "None",
}
# Legacy: the masker used to send [[CLIENT_1]]-style tokens to the model. It now sends realistic
# stand-ins and never emits these, but text written before the change may still hold one, so
# the lint keeps flagging them (there is no masker here to recognise a stand-in).
_MASKER_KINDS = "CLIENT|AUDIT|TERM|IP|EMAIL|URL|LIT"
# (name, pattern, severity): the first match wins, so errors are listed before warnings.
PLACEHOLDERS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("lorem ipsum", re.compile(r"lorem\s+ipsum", re.IGNORECASE), "error"),
    ("TBD", re.compile(r"\bTBD\b", re.IGNORECASE), "error"),
    ("xxx", re.compile(r"(?<!\S)x{4,}(?![^\s.,;:!?)\]])", re.IGNORECASE), "error"),
    (
        "[[...]]",
        re.compile(
            rf"\[\[\s{{0,4}}(?:{_MASKER_KINDS})[\s_\-.\\]{{1,6}}[0-9]{{1,9}}\s{{0,4}}\]\]",
            re.IGNORECASE,
        ),
        "error",
    ),
    ("TODO", re.compile(r"\bTODO\b", re.IGNORECASE), "warning"),
)
IMG_TAG = re.compile(r"<img\b", re.IGNORECASE)
NUMBER = re.compile(r"\d{1,2}(?:\.\d{1,2})?")
LANGUAGE = re.compile(r"^(auto|[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,2})$")
BLOCK_TAGS = frozenset("p br li ul ol div h1 h2 h3 h4 h5 h6 tr td th table pre blockquote".split())
SKIP_TAGS = frozenset({"script", "style"})
CODE_TAGS = SKIP_TAGS | {"pre", "code"}
TEXT_RULES = (
    ("description", "EMPTY_DESCRIPTION", "error", "Description is empty"),
    ("observation", "EMPTY_OBSERVATION", "warning", "Observation is empty"),
    ("remediation", "EMPTY_REMEDIATION", "error", "Remediation is empty"),
)


@dataclass(frozen=True)
class Issue:
    severity: str  # 'error' | 'warning' | 'info'
    code: str
    message: str
    finding_id: str
    finding_title: str
    field: str = ""


@dataclass(frozen=True)
class _Ref:
    """Who an issue is about, already clipped so it can be echoed safely."""

    finding_id: str = ""
    finding_title: str = ""

    def issue(self, severity: str, code: str, message: str, field: str = "") -> Issue:
        return Issue(severity, code, message, self.finding_id, self.finding_title, _clip(field))


@dataclass(frozen=True)
class _Custom:
    label: str
    kind: str
    text: Any


# ----------------------------------------------------------------------------- text helpers


def _clip(value: Any, limit: int = ECHO_LIMIT) -> str:
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return ""
    text = " ".join(str(value)[: limit * 4].split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _raw(value: Any) -> str:
    return value[:MAX_TEXT_CHARS] if isinstance(value, str) else ""


class _TextExtractor(HTMLParser):
    def __init__(self, skip_tags: frozenset[str] = SKIP_TAGS) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_tags = skip_tags
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._skip_tags:
            self._skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._skip_tags:
            self._skip = max(0, self._skip - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _plain(value: Any, skip_tags: frozenset[str] = SKIP_TAGS) -> str:
    """Visible text of an HTML field: tags dropped, entities decoded, whitespace collapsed.

    Text inside ``skip_tags`` elements (script/style by default) is left out.
    """
    text = _raw(value)
    if not text:
        return ""
    parser = _TextExtractor(skip_tags)
    try:
        parser.feed(text)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup must not stop a lint; use what we got
        log.debug("html text extraction stopped early", exc_info=True)
    return " ".join("".join(parser.parts).split())


def _value_text(value: Any, skip_tags: frozenset[str] = SKIP_TAGS) -> str:
    if isinstance(value, str):
        return _plain(value, skip_tags)
    if isinstance(value, (list, tuple)):
        return ", ".join(_plain(item, skip_tags) for item in value if isinstance(item, str))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not _plain(value)
    if isinstance(value, (list, tuple, dict, set)):
        return all(_is_blank(item) for item in value) if not isinstance(value, dict) else not value
    return False


def _ident(value: Any) -> str:
    return _clip(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""


# --------------------------------------------------------------------------- per-field checks


def _poc_issues(ref: _Ref, finding: Mapping[str, Any]) -> list[Issue]:
    poc = finding.get("poc")
    if IMG_TAG.search(_raw(poc)):
        return []
    if not _plain(poc):
        return [ref.issue("error", "MISSING_POC", "Proof of concept is empty", "poc")]
    return [ref.issue("warning", "NO_SCREENSHOT", "Proof of concept has no screenshot", "poc")]


def _text_issues(ref: _Ref, finding: Mapping[str, Any]) -> list[Issue]:
    return [
        ref.issue(severity, code, message, field)
        for field, code, severity, message in TEXT_RULES
        if not _plain(finding.get(field))
    ]


def _reference_issues(ref: _Ref, finding: Mapping[str, Any]) -> list[Issue]:
    refs = finding.get("references")
    items = refs if isinstance(refs, (list, tuple)) else []
    if any(isinstance(item, str) and item.strip() for item in items):
        return []
    return [ref.issue("warning", "NO_REFERENCES", "No references listed", "references")]


def _cvss_issues(ref: _Ref, finding: Mapping[str, Any]) -> list[Issue]:
    vector = finding.get("cvssv3")
    if _is_blank(vector) or parse_vector(vector) is not None:
        return []
    return [ref.issue("error", "INVALID_CVSS", "CVSS v3 vector is not valid", "cvssv3")]


# ------------------------------------------------------------------------------ custom fields


def _custom_fields(finding: Mapping[str, Any]) -> list[_Custom]:
    raw = finding.get("customFields")
    if not isinstance(raw, (list, tuple)):
        return []
    found = []
    for entry in raw:
        definition = entry.get("customField") if isinstance(entry, Mapping) else None
        if not isinstance(definition, Mapping):
            continue
        label, kind = definition.get("label"), definition.get("fieldType")
        found.append(
            _Custom(
                label if isinstance(label, str) else "",
                kind.lower() if isinstance(kind, str) else "",
                entry.get("text"),
            )
        )
    return found


def _empty_custom_issues(ref: _Ref, customs: Iterable[_Custom]) -> list[Issue]:
    issues = []
    for custom in customs:
        if custom.kind in FILLABLE_TYPES and _is_blank(custom.text):
            name = _clip(custom.label)
            message = f"Custom field '{name}' is empty" if name else "A custom field is empty"
            issues.append(ref.issue("warning", "EMPTY_CUSTOM_FIELD", message, custom.label))
    return issues


def _number(text: Any) -> float | None:
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        value = float(text)
    else:
        plain = _value_text(text).strip()
        if not NUMBER.fullmatch(plain):
            return None
        value = float(plain)
    return value if 0 <= value <= 10 else None


def _reference_severity(cvss: Cvss | None, scores: list[tuple[_Custom, float]]) -> str | None:
    if cvss is not None:
        return cvss.severity
    return severity_of(scores[0][1]) if scores else None


def _score_fields(customs: Iterable[_Custom]) -> list[tuple[_Custom, float]]:
    pairs = [(c, _number(c.text)) for c in customs if is_score_label(c.label)]
    return [(c, n) for c, n in pairs if n is not None]


def _severity_issues(ref: _Ref, finding: Mapping[str, Any], customs: list[_Custom]) -> list[Issue]:
    cvss = parse_vector(finding.get("cvssv3"))
    scores = _score_fields(customs)
    expected = _reference_severity(cvss, scores)
    issues = []
    for custom, score in scores:
        if cvss is not None and abs(score - cvss.score) > SCORE_TOLERANCE:
            message = f"Score {score} does not match the CVSS vector ({cvss.score})"
            issues.append(ref.issue("error", "SEVERITY_MISMATCH", message, custom.label))
    for custom in customs:
        stated = SEVERITY_WORDS.get(_value_text(custom.text).lower())
        if not is_severity_label(custom.label) or stated is None or expected is None:
            continue
        if stated != expected:
            message = f"Severity says {stated} but the CVSS data gives {expected}"
            issues.append(ref.issue("error", "SEVERITY_MISMATCH", message, custom.label))
    return issues


# -------------------------------------------------------------------------------- placeholders


def _placeholder_kind(text: str) -> tuple[str, str] | None:
    """``(name, severity)`` of the first placeholder pattern found in ``text``."""
    for name, pattern, severity in PLACEHOLDERS:
        if pattern.search(text):
            return name, severity
    return None


def _placeholder_issues(
    ref: _Ref, finding: Mapping[str, Any], customs: list[_Custom]
) -> list[Issue]:
    """Placeholder text per field; code samples (``pre``/``code``) are not prose and are skipped."""
    issues = []
    fields = [(name, finding.get(name)) for name in PLACEHOLDER_FIELDS]
    fields += [(c.label, c.text) for c in customs if c.kind in FILLABLE_TYPES]
    for field, value in fields:
        found = _placeholder_kind(_value_text(value, CODE_TAGS))
        if found is None and field in NA_FIELDS and _value_text(value).strip().lower() in NA_VALUES:
            found = ("N/A", "error")
        if found is not None:
            message = f"Placeholder text ({found[0]}) left in {_clip(field) or 'a field'}"
            issues.append(ref.issue(found[1], "PLACEHOLDER_TEXT", message, field))
    return issues


# -------------------------------------------------------------------------------- duplicates


def _norm_title(value: Any) -> str:
    return " ".join(_raw(value)[: MAX_TITLE_CHARS * 2].lower().split())[:MAX_TITLE_CHARS]


def _is_near(a: str, b: str) -> bool:
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return (
        matcher.real_quick_ratio() >= NEAR_DUPLICATE_RATIO
        and matcher.quick_ratio() >= NEAR_DUPLICATE_RATIO
        and matcher.ratio() >= NEAR_DUPLICATE_RATIO
    )


def _near_match(title: str, pool: Sequence[tuple[str, int]], skip: bool) -> int | None:
    """Index of the first pooled title that is a near-duplicate of ``title`` (None if ``skip``)."""
    if skip:
        return None
    return next((i for n, i in pool if _is_near(title, n)), None)


def _duplicate_issues(refs: Sequence[_Ref], findings: Sequence[Mapping[str, Any]]) -> list[Issue]:
    """Same-title (all findings) and near-duplicate (first ``MAX_DUPLICATE_TITLES``) warnings.

    The near-duplicate comparison is quadratic, so it is capped in titles, characters and wall
    time; a TRUNCATED info issue says so when something was not compared.
    """
    issues: list[Issue] = []
    exact: dict[str, int] = {}
    pool: list[tuple[str, int]] = []
    started = time.monotonic()
    capped = False
    for index, finding in enumerate(findings):
        title = _norm_title(finding.get("title"))
        if not title:
            continue
        skip = time.monotonic() - started > DUPLICATE_DEADLINE or len(pool) >= MAX_DUPLICATE_TITLES
        first = exact.get(title)
        near = first if first is not None else _near_match(title, pool, skip)
        capped = capped or (skip and near is None)
        if near is None:
            exact[title] = index
            if len(pool) < MAX_DUPLICATE_TITLES:
                pool.append((title, index))
            continue
        kind = "Same title as" if first is not None else "Near-duplicate title of"
        issues.append(
            refs[index].issue("warning", "DUPLICATE_TITLE", f"{kind} finding #{near + 1}", "title")
        )
    if capped:
        message = f"Only the first {MAX_DUPLICATE_TITLES} titles were compared for near-duplicates"
        issues.append(_Ref().issue("info", "TRUNCATED", message))
    return issues


# ------------------------------------------------------------------------------------ public


def _finding_ref(finding: Mapping[str, Any]) -> _Ref:
    return _Ref(_ident(finding.get("_id")), _clip(finding.get("title")))


def _lint_finding(ref: _Ref, finding: Mapping[str, Any]) -> list[Issue]:
    customs = _custom_fields(finding)
    return [
        *_poc_issues(ref, finding),
        *_text_issues(ref, finding),
        *_reference_issues(ref, finding),
        *_cvss_issues(ref, finding),
        *_severity_issues(ref, finding, customs),
        *_empty_custom_issues(ref, customs),
        *_placeholder_issues(ref, finding, customs),
    ]


def _audit_level_issues(audit: Mapping[str, Any], findings: Sequence[Any]) -> list[Issue]:
    issues = []
    if _is_blank(audit.get("template")):
        issues.append(_Ref().issue("warning", "NO_TEMPLATE", "Audit has no template set"))
    if not findings:
        issues.append(_Ref().issue("error", "NO_FINDINGS", "Audit has no findings"))
    return issues


def _usable_findings(audit: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], bool]:
    raw = audit.get("findings")
    items = list(raw) if isinstance(raw, (list, tuple)) else []
    return [f for f in items[:MAX_FINDINGS] if isinstance(f, Mapping)], len(items) > MAX_FINDINGS


def lint_audit(audit: Mapping[str, Any]) -> list[Issue]:
    """Lint a PwnDoc audit document; returns issues in a stable order, never raises."""
    safe = audit if isinstance(audit, Mapping) else {}
    findings, truncated = _usable_findings(safe)
    refs = [_finding_ref(f) for f in findings]
    issues = _audit_level_issues(safe, findings)
    for ref, finding in zip(refs, findings, strict=True):
        issues.extend(_lint_finding(ref, finding))
    issues.extend(_duplicate_issues(refs, findings))
    if truncated:
        message = f"Only the first {MAX_FINDINGS} findings were checked"
        issues.append(_Ref().issue("info", "TRUNCATED", message))
    return issues


def summarize(issues: Iterable[Issue]) -> dict[str, int]:
    """Issue counts by severity; error/warning/info are always present."""
    counts = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        counts[issue.severity] = counts.get(issue.severity, 0) + 1
    return counts


# --------------------------------------------------------------------------------- spelling


def _host_is_blocked(host: str, *, resolve: bool = True) -> bool:
    """Ask the network guard (imported lazily); if it is unavailable the host counts as blocked."""
    try:
        from pwndoc_ai import netguard
    except ImportError:
        log.warning("network guard unavailable; LanguageTool request refused")
        return True
    return bool(netguard.host_is_blocked(host, resolve=resolve))


def _check_endpoint(url: Any, *, resolve: bool = True) -> str | None:
    """The ``/v2/check`` URL for a configured LanguageTool address, or None if it is unusable.

    Unusable means not http(s), no host, or a host the network guard blocks (link-local, cloud
    metadata, ...). The guard is asked on every call, not only when the setting was saved.
    ``resolve=False`` leaves the name lookup to the caller (see :func:`_checked_target`).
    """
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host or _host_is_blocked(host, resolve=resolve):
        return None
    path = parts.path.rstrip("/")
    if not path.endswith("/v2/check"):
        path += "/v2/check"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _checked_target(url: Any, *, pinned: bool) -> SafeTarget | None:
    """The guarded LanguageTool endpoint, or None when it must not be used.

    ``pinned`` (no caller-supplied transport) looks the name up once and keeps the addresses that
    were checked: the connection goes to those, so the HTTP client never does a second lookup that
    an attacker's DNS could answer differently. A name that does not resolve is refused then.
    """
    endpoint = _check_endpoint(url, resolve=not pinned)
    if endpoint is None:
        return None
    try:
        target = inspect_http_url(endpoint, "languagetool_url", resolve=pinned)
    except ValueError:
        return None
    return None if pinned and not target.addresses else target


def _read_matches(response: httpx.Response) -> list[Any]:
    response.raise_for_status()
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("LanguageTool response too large")
    matches = json.loads(bytes(body)).get("matches")
    if not isinstance(matches, list):
        raise ValueError("LanguageTool response has no match list")
    return matches


def _post_check(
    client: httpx.Client,
    target: SafeTarget,
    text: str,
    language: str,
    timeout: float | None = None,
    *,
    pinned: bool = False,
) -> list[Any]:
    options: dict[str, Any] = {"data": {"text": text, "language": language}}
    if timeout is not None:
        options["timeout"] = timeout
    failure: httpx.HTTPError | None = None
    for request in pinned_requests(client, target, "POST", pinned=pinned, **options):
        try:
            response = client.send(request, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            failure = exc  # try the next checked address
            continue
        try:
            return _read_matches(response)
        finally:
            response.close()
    raise failure or httpx.ConnectError("no address to connect to")


def _match_issue(ref: _Ref, field: str, text: str, match: Any) -> Issue | None:
    if not isinstance(match, Mapping):
        return None
    offset, length = match.get("offset"), match.get("length")
    if not (isinstance(offset, int) and isinstance(length, int)):
        return None
    if isinstance(offset, bool) or offset < 0 or length < 1 or offset + length > len(text):
        return None
    advice = _clip(match.get("message"), 40) or "possible mistake"
    word = _clip(text[offset : offset + length], 20)
    return ref.issue("info", "SPELLING", f"'{word}': {advice}", field)


def _spelling_texts(findings: Sequence[Any]) -> Iterable[tuple[_Ref, str, str]]:
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        ref = _finding_ref(finding)
        for field in SPELLING_FIELDS:
            text = _plain(finding.get(field))[:MAX_CHECK_CHARS]
            if text:
                yield ref, field, text


def _check_one(
    client: httpx.Client,
    target: SafeTarget,
    item: tuple[_Ref, str, str],
    lang: str,
    wait: float,
    pinned: bool,
) -> list[Issue]:
    """Issues LanguageTool reports for one field text (raises on a service failure)."""
    ref, field, text = item
    matches = _post_check(client, target, text, lang, wait, pinned=pinned)
    return [i for i in (_match_issue(ref, field, text, m) for m in matches) if i is not None]


def check_spelling(
    findings: Sequence[Mapping[str, Any]],
    languagetool_url: str,
    *,
    language: str = "en-US",
    transport: httpx.BaseTransport | None = None,
    max_issues: int = 100,
    timeout: float = 8.0,
    deadline: float = SPELLING_DEADLINE,
) -> list[Issue] | None:
    """Optional LanguageTool check; ``info`` issues only.

    Returns a list (possibly empty) when the check ran, and ``None`` when the service failed or
    its URL was refused: callers must not treat that as "no mistakes" nor cache it. Nothing to
    check (no findings, ``max_issues <= 0``) is an empty list. At most ``MAX_SPELLING_REQUESTS``
    requests and ``deadline`` seconds in total: what was found by then is returned.
    """
    if max_issues <= 0 or not isinstance(findings, (list, tuple)):
        return []
    pinned = transport is None
    target = _checked_target(languagetool_url, pinned=pinned)
    if target is None:
        return None
    lang = language if isinstance(language, str) and LANGUAGE.match(language) else "auto"
    issues: list[Issue] = []
    started = time.monotonic()
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            for sent, item in enumerate(_spelling_texts(findings)):
                left = deadline - (time.monotonic() - started)
                if sent >= MAX_SPELLING_REQUESTS or len(issues) >= max_issues or left <= 0:
                    break
                found = _check_one(client, target, item, lang, min(timeout, left), pinned)
                issues.extend(found[: max_issues - len(issues)])
    except Exception as exc:  # noqa: BLE001 - spelling is optional; any failure means no result
        log.warning("LanguageTool check failed: %s", type(exc).__name__)
        return None
    return issues
