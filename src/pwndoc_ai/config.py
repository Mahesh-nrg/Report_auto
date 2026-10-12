"""Persistent, validated settings.

Settings live in ``<data_dir>/config.json`` (mode 0600). Environment variables override the
file so secrets can be injected by Docker/CI without ever being written to disk. Settings are
frozen dataclasses: every update produces a new instance.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

DEFAULT_DATA_DIR = "data"
SECRET_FIELDS = ("llm_api_key",)
MASK = "********"

# Environment variable -> settings field.
ENV_OVERRIDES = {
    "PWNDOC_URL": "pwndoc_url",
    "PWNDOC_VERIFY_TLS": "verify_tls",
    "LLM_BASE_URL": "llm_base_url",
    "LLM_API_KEY": "llm_api_key",
    "LLM_MODEL": "llm_model",
}


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
    max_output_tokens: int = 4096
    use_web_search: bool = False  # off by default: finding titles would leave your network
    web_results: int = 3
    use_rag: bool = False
    # Custom-field labels whose values are hidden from the LLM and never modified by it.
    redact_field_labels: tuple[str, ...] = field(default_factory=tuple)
    # Free text appended to the system prompt (house style, language, client tone...).
    extra_instructions: str = ""

    def public(self) -> dict[str, Any]:
        """Dict safe to return to a browser: secrets are replaced by a mask."""
        data = asdict(self)
        data["redact_field_labels"] = list(self.redact_field_labels)
        for name in SECRET_FIELDS:
            data[name + "_set"] = bool(data[name])
            data[name] = MASK if data[name] else ""
        return data

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_model and self.llm_api_key)


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


def _labels(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = value.replace(",", "\n").splitlines()
    cleaned = [str(v).strip() for v in (value or []) if str(v).strip()]
    return tuple(dict.fromkeys(cleaned))


_NORMALISERS = {
    "pwndoc_url": lambda v: _validate_url(v, "pwndoc_url"),
    "llm_base_url": lambda v: _validate_url(v, "llm_base_url"),
    "llm_api_key": lambda v: str(v or "").strip(),
    "llm_model": lambda v: str(v or "").strip(),
    "verify_tls": _to_bool,
    "use_web_search": _to_bool,
    "use_rag": _to_bool,
    "temperature": lambda v: _clamp(v, "temperature", 0, 2, float),
    "max_output_tokens": lambda v: _clamp(v, "max_output_tokens", 256, 65536, int),
    "web_results": lambda v: _clamp(v, "web_results", 0, 10, int),
    "redact_field_labels": _labels,
    "extra_instructions": lambda v: str(v or "").strip()[:4000],
}


def apply_patch(
    current: Settings, patch: dict[str, Any], reset_key_on_url_change: bool = True
) -> Settings:
    """Return a new Settings with ``patch`` applied. Unknown keys are rejected.

    A secret sent back as the mask (or an empty string) keeps the stored value.
    """
    known = {f.name for f in fields(Settings)}
    unknown = set(patch) - known - {n + "_set" for n in SECRET_FIELDS}
    if unknown:
        raise SettingsError(f"unknown settings: {', '.join(sorted(unknown))}")
    changes: dict[str, Any] = {}
    for key, value in patch.items():
        if key not in known:
            continue
        if key in SECRET_FIELDS and value in (MASK, None, ""):
            continue
        changes[key] = _NORMALISERS[key](value)
    updated = replace(current, **changes)
    # A new LLM endpoint must never inherit the stored key (it would be sent to that host).
    key_kept = "llm_api_key" not in changes
    if reset_key_on_url_change and updated.llm_base_url != current.llm_base_url and key_kept:
        updated = replace(updated, llm_api_key="")
    return updated


def _from_env(env: dict[str, str]) -> dict[str, Any]:
    return {field_: env[var] for var, field_ in ENV_OVERRIDES.items() if env.get(var)}


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
            return apply_patch(Settings(), data, reset_key_on_url_change=False)
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
        return apply_patch(stored, _from_env(self._env), reset_key_on_url_change=False)

    @property
    def settings(self) -> Settings:
        with self._lock:
            return self._settings

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
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp, self.path)
