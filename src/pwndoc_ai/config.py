"""Persistent, validated settings.

Settings live in ``<data_dir>/config.json`` (mode 0600). Environment variables override the
file so secrets can be injected by Docker/CI without ever being written to disk. Settings are
frozen dataclasses: every update produces a new instance.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config_fonts import FONT_ENV
from .config_fonts import normalisers as font_normalisers
from .embedder import EmbedderError, model_parts
from .netguard import host_is_blocked, inspect_http_url

log = logging.getLogger(__name__)

DEFAULT_DATA_DIR = "data"
RESEARCH_CACHE_DIR = "research"  # under the data dir; shared by the gateway and the CLI
# Never returned by the API: ``public()`` shows a mask, and sending the mask back keeps the value.
SECRET_FIELDS = ("llm_api_key", "notify_webhook_url")
MASK = "********"

# Environment variable -> settings field.
ENV_OVERRIDES = {
    "PWNDOC_URL": "pwndoc_url",
    "PWNDOC_VERIFY_TLS": "verify_tls",
    "LLM_BASE_URL": "llm_base_url",
    "LLM_API_KEY": "llm_api_key",
    "LLM_MODEL": "llm_model",
    "PWNDOC_AI_EMBEDDER": "embed_backend",
    "PWNDOC_AI_OLLAMA_MODEL": "ollama_model",
    "PWNDOC_AI_OLLAMA_URL": "ollama_url",
    "PWNDOC_AI_OLLAMA_IDLE": "ollama_idle_seconds",
    **FONT_ENV,
}
# Embedder variables predate the settings: a bad value is ignored (with a warning), not fatal.
LENIENT_ENV_FIELDS = frozenset(
    {"embed_backend", "ollama_model", "ollama_url", "ollama_idle_seconds", *FONT_ENV.values()}
)
EMBED_BACKENDS = ("auto", "ollama", "onnx")
DEFAULT_OLLAMA_MODEL = "all-minilm"
OLLAMA_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}")
OLLAMA_IDLE_MIN, OLLAMA_IDLE_MAX = 30, 3600


def is_blocked_host(host: str | None) -> bool:
    """True for link-local / unspecified / multicast / cloud-metadata hosts in any spelling.

    Thin wrapper over :func:`netguard.host_is_blocked` without name resolution; a missing host
    is not "blocked" here (callers check for one separately).
    """
    return bool(host) and host_is_blocked(host, resolve=False)


class SettingsError(ValueError):
    """Raised when a settings patch is invalid."""


@dataclass(frozen=True)
class Settings:
    pwndoc_url: str = "https://localhost:8443"
    verify_tls: bool = True  # loopback upstreams are exempt, see effective_verify_tls
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    temperature: float = 0.2
    max_output_tokens: int = 16384  # long PoCs need room; models cap it themselves (see llm.py)
    use_web_search: bool = False  # off by default: finding titles would leave your network
    web_results: int = 3
    use_rag: bool = False
    # Custom-field labels whose values are hidden from the LLM and never modified by it.
    redact_field_labels: tuple[str, ...] = field(default_factory=tuple)
    # Free text appended to the system prompt (house style, language, client tone...).
    extra_instructions: str = ""
    # Tried in order when the main model is filtered, rate-limited, unavailable or unparsable.
    llm_fallback_models: tuple[str, ...] = field(default_factory=tuple)
    # Replace client/company names, domains/hosts, IP addresses and e-mail addresses by
    # realistic stand-ins before the LLM sees them, and restore the real values in its answer.
    # URL paths, query names, payloads, headers and tokens are sent as they are.
    mask_sensitive: bool = True
    # Optional Slack/Teams/generic incoming webhook told when a Finish report run ends.
    notify_webhook_url: str = ""
    # Optional LanguageTool server (e.g. http://localhost:8010) for spelling checks in the QA pass.
    languagetool_url: str = ""
    # Show the AI wording of findings already in PwnDoc's vulnerability library as reference.
    use_library: bool = True
    # Downloads first store the normalised findings (CVSS 3.1, severity, CWE/CVE, N/A fills,
    # report order) behind the automatic backup; off = export as stored and only report issues.
    normalise_before_download: bool = True
    # Reuse web/local research for the same finding title for this many days (0 = off).
    research_cache_days: int = 14
    # Embeddings (web results and local documents) come from one small Ollama model that is
    # started only while it is needed. ``auto`` uses it when installed, else the built-in ONNX one.
    embed_backend: str = "auto"
    ollama_model: str = DEFAULT_OLLAMA_MODEL
    ollama_idle_seconds: int = 300  # the server stops itself after this long without use
    ollama_url: str = ""  # "" = start our own; a URL = use that server and never stop it
    # Fonts applied to PwnDoc audit downloads and refined copies ("" / 0 = keep the document's).
    default_font: str = ""
    default_font_size: float = 0.0
    heading_font: str = ""
    heading_font_size: float = 0.0
    mono_font: str = ""
    mono_font_size: float = 0.0

    def public(self) -> dict[str, Any]:
        """Dict safe to return to a browser: secrets are replaced by a mask."""
        data = asdict(self)
        data["redact_field_labels"] = list(self.redact_field_labels)
        data["llm_fallback_models"] = list(self.llm_fallback_models)
        for name in SECRET_FIELDS:
            data[name + "_set"] = bool(data[name])
            data[name] = MASK if data[name] else ""
        return data

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_model and self.llm_api_key)

    @property
    def model_chain(self) -> tuple[str, ...]:
        """The main model followed by the fallbacks, without blanks or repeats."""
        return tuple(dict.fromkeys(m for m in (self.llm_model, *self.llm_fallback_models) if m))


def effective_verify_tls(settings: Settings) -> bool:
    """Verify the PwnDoc certificate, except for a loopback upstream.

    Stock PwnDoc ships a self-signed certificate; on loopback there is no network path for a
    man-in-the-middle, so verification adds friction without protection. Remote upstreams are
    verified unless ``verify_tls`` is explicitly turned off.
    """
    host = urlparse(settings.pwndoc_url).hostname or ""
    if host in {"localhost", "127.0.0.1", "::1"}:
        return False
    return settings.verify_tls


def _validate_url(value: Any, name: str) -> str:
    text = str(value or "").strip().rstrip("/")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SettingsError(f"{name} must be a http(s) URL")
    return text


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _clamp(value: Any, name: str, low: float, high: float, cast: type) -> Any:
    try:
        number = cast(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{name} must be a number") from exc
    if not low <= number <= high:
        raise SettingsError(f"{name} must be between {low} and {high}")
    return number


def _guarded_url(value: Any, name: str, *, optional: bool, resolve: bool) -> str:
    """A http(s) URL outside link-local / cloud-metadata space (host names are resolved)."""
    text = str(value or "").strip().rstrip("/")
    if optional and not text:
        return ""
    try:
        return inspect_http_url(text, name, resolve=resolve).url
    except ValueError as exc:
        raise SettingsError(str(exc)) from exc


def _embed_backend(value: Any) -> str:
    choice = value.strip().lower() if isinstance(value, str) else ""
    if choice not in EMBED_BACKENDS:
        raise SettingsError(f"embed_backend must be one of {', '.join(EMBED_BACKENDS)}")
    return choice


def _ollama_model(value: Any) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise SettingsError("ollama_model must be text")
    if not text:
        return DEFAULT_OLLAMA_MODEL
    if not OLLAMA_MODEL_NAME.fullmatch(text) or ".." in text:
        raise SettingsError("ollama_model must be an Ollama model name such as all-minilm")
    try:
        model_parts(text)  # also refuses a model of any registry but the default one
    except EmbedderError as exc:
        raise SettingsError(f"ollama_model: {exc}") from None
    return text


def _ollama_idle(value: Any) -> int:
    """Seconds, clamped into the supported range (a number is required)."""
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value)
        if not math.isfinite(number):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise SettingsError("ollama_idle_seconds must be a number") from exc
    return max(OLLAMA_IDLE_MIN, min(OLLAMA_IDLE_MAX, int(number)))


def _labels(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = value.replace(",", "\n").splitlines()
    cleaned = [str(v).strip() for v in (value or []) if str(v).strip()]
    return tuple(dict.fromkeys(cleaned))


_NORMALISERS = {
    "pwndoc_url": lambda v: _validate_url(v, "pwndoc_url"),
    "llm_api_key": lambda v: str(v or "").strip(),
    "llm_model": lambda v: str(v or "").strip(),
    "verify_tls": _to_bool,
    "use_web_search": _to_bool,
    "use_rag": _to_bool,
    "temperature": lambda v: _clamp(v, "temperature", 0, 2, float),
    "max_output_tokens": lambda v: _clamp(v, "max_output_tokens", 256, 1_000_000, int),
    "web_results": lambda v: _clamp(v, "web_results", 0, 10, int),
    "redact_field_labels": _labels,
    "llm_fallback_models": _labels,
    "mask_sensitive": _to_bool,
    "use_library": _to_bool,
    "normalise_before_download": _to_bool,
    "research_cache_days": lambda v: _clamp(v, "research_cache_days", 0, 365, int),
    "extra_instructions": lambda v: str(v or "").strip()[:4000],
    "embed_backend": _embed_backend,
    "ollama_model": _ollama_model,
    "ollama_idle_seconds": _ollama_idle,
    **font_normalisers(SettingsError),
}


# URL settings checked against the SSRF guard: name -> may be left empty.
_GUARDED_URLS = {
    "llm_base_url": False,
    "notify_webhook_url": True,
    "languagetool_url": True,
    "ollama_url": True,
}
_SECRET_EXTRAS = tuple(f"{n}{suffix}" for n in SECRET_FIELDS for suffix in ("_set", "_clear"))


def _normalise(key: str, value: Any, resolve: bool) -> Any:
    if key in _GUARDED_URLS:
        return _guarded_url(value, key, optional=_GUARDED_URLS[key], resolve=resolve)
    return _NORMALISERS[key](value)


def apply_patch(
    current: Settings,
    patch: dict[str, Any],
    reset_key_on_url_change: bool = True,
    *,
    resolve: bool = True,
) -> Settings:
    """Return a new Settings with ``patch`` applied. Unknown keys are rejected.

    A secret sent back as the mask (or an empty string) keeps the stored value; sending
    ``<name>_clear: true`` removes it. ``resolve`` looks host names up for the SSRF guard (turn
    it off for values already stored, so a DNS hiccup at start-up cannot discard the config).
    """
    known = {f.name for f in fields(Settings)}
    unknown = set(patch) - known - set(_SECRET_EXTRAS)
    if unknown:
        raise SettingsError(f"unknown settings: {', '.join(sorted(unknown))}")
    changes: dict[str, Any] = {n: "" for n in SECRET_FIELDS if _to_bool(patch.get(n + "_clear"))}
    for key, value in patch.items():
        if key not in known:
            continue
        if key in SECRET_FIELDS and value in (MASK, None, ""):
            continue
        changes[key] = _normalise(key, value, resolve)
    updated = replace(current, **changes)
    # A new LLM endpoint must never inherit the stored key (it would be sent to that host).
    key_kept = "llm_api_key" not in changes
    if reset_key_on_url_change and updated.llm_base_url != current.llm_base_url and key_kept:
        updated = replace(updated, llm_api_key="")
    return updated


def _from_env(env: dict[str, str]) -> dict[str, Any]:
    picked: dict[str, Any] = {}
    for var, field_ in ENV_OVERRIDES.items():
        if not env.get(var):
            continue
        if field_ in LENIENT_ENV_FIELDS:
            try:
                _normalise(field_, env[var], resolve=False)
            except SettingsError:
                log.warning("ignoring %s: the value is not valid", var)  # never echo the value
                continue
        picked[field_] = env[var]
    return picked


class ConfigStore:
    """Thread-safe load/save of :class:`Settings` with environment overrides."""

    def __init__(
        self, data_dir: str | os.PathLike[str] | None = None, env: dict[str, str] | None = None
    ):
        self.data_dir = Path(data_dir or os.environ.get("PWNDOC_AI_DATA_DIR", DEFAULT_DATA_DIR))
        self.path = self.data_dir / "config.json"
        self._env = dict(os.environ if env is None else env)
        self._lock = threading.Lock()
        # What the file holds (never contains environment values) vs. what is in effect.
        self._stored = self._load_stored()
        self._settings = self._overlay_env(self._stored)

    def _load_stored(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("config root must be an object")
            return apply_patch(Settings(), data, reset_key_on_url_change=False, resolve=False)
        except OSError as exc:
            log.warning("cannot read %s (%s); using defaults for this run", self.path, exc)
        except ValueError as exc:
            quarantine = self.path.with_name(self.path.name + ".bad")
            log.warning(
                "%s is invalid (%s); moved to %s, using defaults", self.path, exc, quarantine
            )
            try:
                os.replace(self.path, quarantine)
            except OSError:
                log.warning("could not move the invalid config aside")
        return Settings()

    def _overlay_env(self, stored: Settings) -> Settings:
        return apply_patch(
            stored, _from_env(self._env), reset_key_on_url_change=False, resolve=False
        )

    @property
    def settings(self) -> Settings:
        with self._lock:
            return self._settings

    @property
    def env(self) -> dict[str, str]:
        """A copy of the environment this store applies (the embedder reads its own from it)."""
        return dict(self._env)

    def update(self, patch: dict[str, Any]) -> Settings:
        with self._lock:
            stored = apply_patch(self._stored, patch)
            self._guard_env_key(stored)
            self._persist(stored)
            self._stored = stored
            self._settings = self._overlay_env(stored)  # env values keep winning, visibly
            return self._settings

    def _guard_env_key(self, candidate: Settings) -> None:
        """An env-supplied key must not be redirected to a different LLM host from the UI."""
        env_sets_key = bool(self._env.get("LLM_API_KEY"))
        env_sets_url = bool(self._env.get("LLM_BASE_URL"))
        url_changed = candidate.llm_base_url != self._stored.llm_base_url
        if env_sets_key and not env_sets_url and url_changed:
            raise SettingsError(
                "llm_base_url cannot be changed while LLM_API_KEY comes from the environment; "
                "set LLM_BASE_URL there as well"
            )

    def _persist(self, settings: Settings) -> None:
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = asdict(settings)
        payload["redact_field_labels"] = list(settings.redact_field_labels)
        payload["llm_fallback_models"] = list(settings.llm_fallback_models)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp, self.path)
