"""LLM fallback of the importer: fill only what the deterministic reader left blank.

Two kinds of call, on two sides of the masking boundary (``maskflow``):

* one **header** call (unmasked, input = ``header_text`` only) for a blank company, start or end
  date, or scope;
* **finding** calls (masked) for a blank title or severity, and for splitting the combined
  "Description" cell into description and observation.

Every answer is untrusted. It must be a dict with only the keys that were asked for, every value
is type- and length-checked, and every value must be grounded in the document: a company and each
scope item occur in ``header_text``, a date's numbers occur there, a title's words occur in the
finding text, a severity is written there or follows from the CVSS score, and a split is an exact
partition of the original paragraphs (the model only returns the paragraph number where the
observation starts). A value that fails is counted in ``rejected`` and the blank is kept.

Nothing here logs prompts, answers, names or document text.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from .. import cvss
from ..llm import LlmAuthError, LlmError
from . import completeness, limits, maskflow, textdates
from . import fallback_prompts as prompts
from .errors import FallbackRejected
from .model import CompletenessIssue, HeaderFields, ImportedFinding, ImportedReport

MAX_BATCH_ITEMS = 6
MAX_BATCH_CHARS = 20_000
MAX_PARAGRAPH_CHARS = 1500
MAX_CONTEXT_CHARS = 1500
MAX_SPLIT_BLOCKS = 40
OUTPUT_TOKENS_PER_ITEM = 150
_BLOCK_TAG = re.compile(r"<(/?)([a-z]+)>")
_VOID_TAGS = frozenset({"br"})
_WORDS = re.compile(r"[^\W_]+")


@dataclass(frozen=True)
class FallbackOutcome:
    report: ImportedReport
    calls: int = 0
    rejected: int = 0
    failed: bool = False
    key_rejected: bool = False


# -- text helpers (reader's allow-list tools, imported at call time) ----------------------------


def _plain(value: str) -> str:
    from . import html as html_tools

    return html_tools.plain_text(value) if value else ""


def _clean(value: str) -> str:
    from . import html as html_tools

    return html_tools.clean_html(value) if value else ""


def _norm(value: str) -> str:
    from . import labels

    return labels.norm(value).casefold()


def _words(value: str) -> set[str]:
    return set(_WORDS.findall(_norm(value)))


def html_blocks(value: str) -> tuple[str, ...]:
    """The top-level blocks (``<p>``, ``<ul>``, ``<ol>``, ``<pre>``) of allow-list HTML, so that
    ``"".join(html_blocks(x)) == clean_html(x)``. Unbalanced input is returned as one block."""
    html = _clean(value)
    blocks, depth, start, cursor = [], 0, 0, 0
    for match in _BLOCK_TAG.finditer(html):
        name = match.group(2)
        if name in _VOID_TAGS:
            continue
        if depth == 0:
            if html[cursor : match.start()].strip():
                return (html,) if html else ()
            start = match.start()
        depth += -1 if match.group(1) else 1
        if depth < 0:
            return (html,)
        if depth == 0:
            blocks.append(html[start : match.end()])
        cursor = match.end()
    if depth or html[cursor:].strip():
        return (html,) if html else ()
    return tuple(blocks)


def _one_line(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        raise FallbackRejected()
    text = " ".join(limits.CONTROL_LINE.sub(" ", value).split())
    if len(text) > limit:
        raise FallbackRejected()
    return text


# -- header ---------------------------------------------------------------------------------------


def _check_company(value: Any, header_text: str, header: HeaderFields) -> str:
    company = _one_line(value, limits.MAX_NAME_CHARS)
    leftovers = set(completeness.leftover_keys()) | {_norm(n) for n in header.leftover_names}
    if not company or _norm(company) not in _norm(header_text) or _norm(company) in leftovers:
        raise FallbackRejected()
    return company


def _check_date(value: Any, header_text: str) -> str:
    text = _one_line(value, 10)
    try:
        parsed = date.fromisoformat(text) if limits.ISO_DATE.fullmatch(text) else None
    except ValueError:
        parsed = None
    if parsed is None:
        raise FallbackRejected()
    if parsed not in textdates.dates_in(header_text):
        raise FallbackRejected()  # only a date the excerpt writes out, never a recombination
    return text


def _check_scope(value: Any, header_text: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > limits.MAX_SCOPE_ITEMS:
        raise FallbackRejected()
    items = tuple(dict.fromkeys(_one_line(v, limits.MAX_SCOPE_CHARS) for v in value))
    haystack = _norm(header_text)
    if any(not item or _norm(item) not in haystack for item in items):
        raise FallbackRejected()
    return items


def _header_values(answer: dict, gaps: Sequence[str], report: ImportedReport):
    """Accepted header values and the number of rejected ones."""
    if set(answer) - set(gaps):
        raise FallbackRejected()
    text, accepted, rejected = report.header_text, {}, 0
    checks: dict[str, Callable[[Any], Any]] = {
        "company": lambda v: _check_company(v, text, report.header),
        "date_start": lambda v: _check_date(v, text),
        "date_end": lambda v: _check_date(v, text),
        "scope": lambda v: _check_scope(v, text),
    }
    for name in gaps:
        value = answer.get(name)
        if value in (None, "", []):
            continue
        try:
            accepted[name] = checks[name](value)
        except FallbackRejected:
            rejected += 1
    start = accepted.get("date_start") or report.header.date_start
    end = accepted.get("date_end") or report.header.date_end
    if start and end and start > end:
        rejected += sum(1 for name in ("date_start", "date_end") if accepted.pop(name, None))
    return accepted, rejected


def _fill_header(report: ImportedReport, llm: Any, extra: str) -> tuple[ImportedReport, int]:
    gaps = prompts.header_gaps(report.header)
    system, user = prompts.header_messages(report.header_text, report.header)
    answer = maskflow.call_header(
        llm, system + prompts.house_block(extra), user, prompts.HEADER_TOOL
    )
    accepted, rejected = _header_values(answer, gaps, report)
    if not accepted:
        return report, rejected
    issue = CompletenessIssue("llm_fields", fields=tuple(accepted))
    header = replace(report.header, **accepted)
    return replace(report, header=header, issues=(*report.issues, issue)), rejected


# -- findings -------------------------------------------------------------------------------------


def _needs(finding: ImportedFinding) -> tuple[str, ...]:
    needs = []
    if not finding.title.strip():
        needs.append("title")
    if not finding.severity:
        needs.append("severity")
    splittable = not finding.observation.strip() and "description_split" not in finding.flags
    if splittable and 2 <= len(html_blocks(finding.description)) <= MAX_SPLIT_BLOCKS:
        needs.append("split")
    return tuple(needs)


def finding_text(finding: ImportedFinding) -> str:
    """All of the finding's own text, the ground truth for title and severity checks."""
    rich = [finding.description, finding.observation, finding.remediation]
    rich += [block.html for block in finding.poc if block.kind == "html"]
    plain = [finding.title, finding.cwe, finding.parameter, finding.location, finding.cvss_score]
    plain += [*finding.urls, *finding.references]
    return "\n".join([*(_plain(v) for v in rich), *plain])


def _item(finding: ImportedFinding, needs: Sequence[str]) -> dict[str, Any]:
    item: dict[str, Any] = {"key": finding.key, "need": list(needs)}
    item.update(title=finding.title, cvss_score=finding.cvss_score, cwe=finding.cwe)
    blocks = html_blocks(finding.description)
    item["paragraphs"] = [
        {"n": n, "text": _plain(block)[:MAX_PARAGRAPH_CHARS]}
        for n, block in enumerate(blocks[:MAX_SPLIT_BLOCKS], start=1)
    ]
    if "title" in needs or "severity" in needs:
        rest = [finding.remediation, *(b.html for b in finding.poc if b.kind == "html")]
        item["context"] = "\n".join(_plain(v) for v in rest)[:MAX_CONTEXT_CHARS]
    return item


def _batches(items: Sequence[dict], max_output_tokens: int) -> list[list[dict]]:
    per_batch = max(1, min(MAX_BATCH_ITEMS, max_output_tokens // OUTPUT_TOKENS_PER_ITEM))
    batches: list[list[dict]] = []
    size = 0
    for item in items:
        length = len(prompts.json_line(item))
        if not batches or len(batches[-1]) >= per_batch or size + length > MAX_BATCH_CHARS:
            batches.append([])
            size = 0
        batches[-1].append(item)
        size += length
    return batches


def _check_title(value: Any, finding: ImportedFinding) -> str:
    title = _one_line(value, limits.MAX_TITLE_CHARS)
    words = _words(title)
    if not words or not words <= _words(finding_text(finding)):
        raise FallbackRejected()
    return title


def labelled_severity(value: str, text: str) -> bool:
    """True when ``text`` prints ``value`` as a severity: ``Severity: High``, ``Risk level -
    High``, ``rated High``, ``High risk`` or ``High severity``; a bare word (``high
    privileges``) is not one."""
    word = re.escape(value)
    label = (
        rf"\b(?:severity|risk(?:\s+(?:level|rating))?|rated(?:\s+as)?)\s*[:=\-\u2013]?\s*{word}\b"
    )
    suffix = rf"\b{word}\s+(?:risk|severity)\b"
    return bool(re.search(f"{label}|{suffix}", text, re.IGNORECASE))


def _check_severity(value: Any, finding: ImportedFinding) -> str:
    """A printed CVSS score decides alone; without one the text must print the severity as a
    labelled value (see ``labelled_severity``)."""
    if not isinstance(value, str) or value not in limits.SEVERITIES:
        raise FallbackRejected()
    try:
        implied = cvss.severity_of(float(finding.cvss_score)) if finding.cvss_score else ""
    except ValueError:
        implied = ""
    grounded = implied == value if implied else labelled_severity(value, finding_text(finding))
    if not grounded:
        raise FallbackRejected()
    return value


def _check_split(value: Any, finding: ImportedFinding) -> dict[str, str]:
    blocks = html_blocks(finding.description)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= len(blocks):
        raise FallbackRejected()
    if value == 0:
        return {}
    if value < 2:
        raise FallbackRejected()
    description, observation = "".join(blocks[: value - 1]), "".join(blocks[value - 1 :])
    joined = _norm(_plain(description) + "\n" + _plain(observation))
    if not observation or joined != _norm(_plain(finding.description)):
        raise FallbackRejected()
    return {"description": description, "observation": observation}


ANSWER_FIELDS = {"title": "title", "severity": "severity", "split": "observation_from"}


def _entry_values(entry: Any, finding: ImportedFinding, needs: Sequence[str]):
    """Accepted values for one finding and the number of rejected fields."""
    allowed = {"key", *(ANSWER_FIELDS[n] for n in needs)}
    if not isinstance(entry, dict) or set(entry) - allowed:
        raise FallbackRejected()
    checks: dict[str, Callable[[Any, ImportedFinding], Any]] = {
        "title": _check_title,
        "severity": _check_severity,
        "observation_from": _check_split,
    }
    values: dict[str, Any] = {}
    rejected = 0
    for name in sorted(set(entry) - {"key"}):
        try:
            result = checks[name](entry[name], finding)
        except FallbackRejected:
            rejected += 1
            continue
        values.update(result if isinstance(result, dict) else {name: result})
    return values, rejected


def _filled(finding: ImportedFinding, values: dict[str, Any]) -> ImportedFinding:
    if not values:
        return finding
    fields = tuple(dict.fromkeys([*finding.llm_fields, *values]))
    flags = finding.flags
    if "observation" in values and "description_split" not in flags:
        flags = (*flags, "description_split")
    return replace(finding, **values, llm_fields=fields, flags=flags)


def _entries(answer: dict, batch: Sequence[dict]) -> dict[str, Any]:
    entries = answer.get("findings")
    if set(answer) - {"findings"} or not isinstance(entries, list):
        raise FallbackRejected()
    keys = [e.get("key") if isinstance(e, dict) else None for e in entries]
    sent = {item["key"] for item in batch}
    if len(set(keys)) != len(keys) or not set(keys) <= sent:
        raise FallbackRejected()
    return dict(zip(keys, entries, strict=True))


@dataclass(frozen=True)
class _Run:
    llm: Any
    masker: Any
    system: str


def _finding_user(items: Sequence[dict]) -> str:
    return prompts.finding_messages(items)[1]


def _run_batch(run: _Run, batch: Sequence[dict], findings: dict[str, ImportedFinding]):
    """Updated findings of one batch and the number of rejected values."""
    answer = maskflow.call_masked_items(
        run.llm, run.masker, run.system, batch, _finding_user, prompts.FINDING_TOOL
    )
    updated, rejected = {}, 0
    for key, entry in _entries(answer, batch).items():
        needs = next(item["need"] for item in batch if item["key"] == key)
        try:
            values, bad = _entry_values(entry, findings[key], needs)
        except FallbackRejected:
            rejected += 1
            continue
        rejected += bad
        updated[key] = _filled(findings[key], values)
    return updated, rejected


# -- orchestration --------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Tally:
    report: ImportedReport
    calls: int = 0
    rejected: int = 0

    def add(self, report: ImportedReport, calls: int = 0, rejected: int = 0) -> _Tally:
        return _Tally(report, self.calls + calls, self.rejected + rejected)


def _header_step(tally: _Tally, llm: Any, settings: Any) -> _Tally:
    report = tally.report
    if not prompts.header_gaps(report.header) or not report.header_text.strip():
        return tally
    try:
        filled, rejected = _fill_header(report, llm, settings.extra_instructions)
    except FallbackRejected:
        return tally.add(report, 1, 1)
    return tally.add(filled, 1, rejected)


def _finding_run(report: ImportedReport, llm: Any, settings: Any, extra_terms, items) -> _Run:
    """The masker for this report's finding calls, with every finding item registered first.

    Raises ValueError when the corpus cannot be registered (the caller sends nothing)."""
    masker = maskflow.finding_masker(
        enabled=settings.mask_sensitive, header=report.header, extra_terms=extra_terms
    )
    maskflow.prepare(masker, items)
    system = prompts.finding_messages(())[0] + prompts.house_block(settings.extra_instructions)
    return _Run(llm=llm, masker=masker, system=system)


def _with_findings(report: ImportedReport, current: dict[str, ImportedFinding]) -> ImportedReport:
    return replace(report, findings=tuple(current[f.key] for f in report.findings))


def _finding_steps(tally: _Tally, llm, settings, extra_terms, cancelled) -> _Tally:
    """Fill findings batch by batch. An LlmError propagates as ``_Stopped`` with the tally of
    what was already accepted, so earlier batches are kept."""
    report = tally.report
    items = [
        _item(f, needs) for f in report.findings if f.include for needs in [_needs(f)] if needs
    ]
    if not items:
        return tally
    if settings.mask_sensitive and not maskflow.has_client_name(report.header):
        return tally.add(_with_issue(report, "llm_skipped_unmasked"))  # fail closed
    try:
        run = _finding_run(report, llm, settings, extra_terms, items)
    except ValueError:  # masking could not be prepared: no finding text leaves the machine
        return tally.add(_with_issue(report, "llm_skipped_unmasked"))
    current = {f.key: f for f in report.findings}
    for batch in _batches(items, settings.max_output_tokens):
        if cancelled():
            break
        try:
            updated, rejected = _run_batch(run, batch, current)
        except FallbackRejected:
            tally = tally.add(tally.report, 1, 1)
            continue
        except LlmError as exc:
            raise _Stopped(tally.add(_with_findings(report, current), 1), exc) from None
        current = {**current, **updated}
        tally = tally.add(_with_findings(report, current), 1, rejected)
    return tally


class _Stopped(Exception):
    """An LlmError with the work accepted before it."""

    def __init__(self, tally: _Tally, error: LlmError) -> None:
        super().__init__("stopped")
        self.tally = tally
        self.error = error


def _with_issue(report: ImportedReport, code: str) -> ImportedReport:
    return replace(report, issues=(*report.issues, CompletenessIssue(code)))


def _failed(tally: _Tally, error: LlmError) -> FallbackOutcome:
    report = _with_issue(tally.report, "llm_failed")
    auth = isinstance(error, LlmAuthError)
    return FallbackOutcome(report, tally.calls, tally.rejected, failed=True, key_rejected=auth)


def fill_gaps(
    report: ImportedReport,
    *,
    llm: Any,
    settings: Any,
    extra_terms: Sequence[str] = (),
    cancelled: Callable[[], bool] = lambda: False,
) -> FallbackOutcome:
    """The report with the reader's blanks filled where a validated LLM answer allows."""
    if llm is None:
        return FallbackOutcome(_with_issue(report, "llm_unavailable"))
    tally = _Tally(report)
    try:
        if not cancelled():
            tally = _header_step(tally, llm, settings)
    except LlmError as exc:
        return _failed(tally.add(tally.report, 1), exc)
    try:
        tally = _finding_steps(tally, llm, settings, extra_terms, cancelled)
    except _Stopped as stop:
        return _failed(stop.tally, stop.error)
    return FallbackOutcome(tally.report, tally.calls, tally.rejected)


__all__ = ["FallbackOutcome", "fill_gaps", "finding_text", "html_blocks"]
