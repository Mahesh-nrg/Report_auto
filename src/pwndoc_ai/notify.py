"""Fire-and-forget webhook notifications (Slack, Teams or any generic incoming webhook).

The webhook URL usually embeds a secret token, so it is never logged: warnings name the host only
(and a refusal names nothing). :func:`notify` never raises; a notification must not be able to
fail a report. The target is checked again right before the request (a host name may resolve
differently than when the setting was saved) and the connection goes to the checked address.
"""

from __future__ import annotations

import logging
import re
import threading
from urllib.parse import urlsplit

import httpx

from .netguard import SafeTarget, inspect_http_url, pinned_requests

log = logging.getLogger(__name__)

MAX_TEXT = 1500
DEFAULT_TIMEOUT = 5.0
_WHITESPACE = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_BARE_AMPERSAND = re.compile(
    r"&(?!(?:amp|lt|gt);)"
)  # leaves entities alone: escaping is idempotent
_MAX_NAME = 120
_MAX_ERROR = 300
_IN_FLIGHT = threading.local()


class _MuteWhileNotifying(logging.Filter):
    """httpx logs every request URL at INFO; a webhook URL is a secret, so mute it meanwhile."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(_IN_FLIGHT, "active", False)


for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).addFilter(_MuteWhileNotifying())


def _escape_markup(text: str, limit: int) -> str:
    """Escape ``& < >`` (Slack/Teams treat ``<...>`` as links and mentions), never cut an entity."""
    escaped = _BARE_AMPERSAND.sub("&amp;", text).replace("<", "&lt;").replace(">", "&gt;")
    if len(escaped) <= limit:
        return escaped
    cut = escaped[:limit]
    start = cut.rfind("&")
    return cut[:start] if start != -1 and ";" not in cut[start:] else cut


def clean_text(text: object, limit: int = MAX_TEXT) -> str:
    """Strip control characters, flatten whitespace, escape chat markup and cap the length.

    Idempotent: cleaning already-clean text changes nothing, so composed messages stay stable.
    """
    if not isinstance(text, str):
        return ""
    flattened = _WHITESPACE.sub(" ", _CONTROL.sub("", text)).strip()[:limit]
    return _escape_markup(flattened, limit)


def _host_of(url: str) -> str:
    """The host only; the path and userinfo of a webhook URL are secrets."""
    try:
        return urlsplit(url).hostname or "unknown-host"
    except ValueError:
        return "unknown-host"


def _post(
    target: SafeTarget, text: str, transport: httpx.BaseTransport | None, timeout: float
) -> httpx.Response:
    pinned = transport is None
    _IN_FLIGHT.active = True
    try:
        with httpx.Client(transport=transport, timeout=timeout, follow_redirects=False) as client:
            failure: httpx.HTTPError | None = None
            for request in pinned_requests(
                client, target, "POST", pinned=pinned, json={"text": text}
            ):
                try:
                    return client.send(request)
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    failure = exc  # try the next checked address
            raise failure or httpx.ConnectError("no address to connect to")
    finally:
        _IN_FLIGHT.active = False


def _refusal(url: str, transport: httpx.BaseTransport | None) -> SafeTarget | None:
    """The checked target, or None (after one generic log line) when it must not be used."""
    try:
        target = inspect_http_url(url, "webhook")
    except ValueError:
        log.warning(
            "notification not sent: the webhook URL is unusable or targets a blocked address"
        )
        return None
    if transport is None and not target.addresses:
        log.warning("notification not sent: the webhook host name did not resolve")
        return None  # connecting would do a second, unchecked lookup
    return target


def notify(
    url: str,
    text: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """POST ``{"text": text}`` to a webhook. True only on a 2xx answer; never raises."""
    if not isinstance(url, str) or not url.strip():
        return False
    target = _refusal(url.strip(), transport)
    if target is None:
        return False
    host = _host_of(target.url)
    try:
        response = _post(target, clean_text(text), transport, timeout)
    except httpx.HTTPError as exc:
        log.warning("notification to %s failed: %s", host, type(exc).__name__)
        return False
    except Exception as exc:  # noqa: BLE001 - a notification must never break the caller
        log.warning("notification to %s failed unexpectedly: %s", host, type(exc).__name__)
        return False
    if not response.is_success:
        log.warning("notification to %s rejected with HTTP %s", host, response.status_code)
        return False
    return True


def format_finish_message(
    *,
    audit_name: str,
    user: str,
    status: str,
    applied: int,
    failed: int,
    report: bool,
    error: str = "",
) -> str:
    """One short human line describing how a finish-report run ended."""
    audit = clean_text(audit_name, _MAX_NAME) or "audit"
    who = clean_text(user, _MAX_NAME) or "unknown user"
    state = clean_text(status, 40) or "finished"
    outcome = "with a report" if report else "without a report"
    line = (
        f'AI report run for "{audit}" by {who}: {state}, '
        f"{applied} applied, {failed} failed, {outcome}"
    )
    detail = clean_text(error, _MAX_ERROR)
    if detail:
        line = f"{line}. Error: {detail}"
    return clean_text(line)
