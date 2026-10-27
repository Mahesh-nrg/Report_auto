"""OpenAI-compatible chat client (OpenAI, OpenRouter, Ollama, LM Studio, vLLM, ...)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from typing import Any

from openai import OpenAI

from .config import Settings
from .netguard import inspect_http_url
from .prompts import TOOL_NAME

REQUEST_TIMEOUT = 180.0
log = logging.getLogger(__name__)

CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class LlmError(RuntimeError):
    pass


class LlmAuthError(LlmError):
    """The provider rejected the credentials (401/403): no other model will do better."""


AUTH_STATUSES = (401, 403)


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens
        )

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


def estimate_tokens(text: str) -> int:
    """Rough fallback (about 4 characters per token) when the provider reports no usage."""
    return max(1, len(text) // 4) if text else 0


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from model text, tolerating code fences and surrounding prose."""
    cleaned = CODE_FENCE.sub("", (text or "").strip())
    try:
        value = json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise LlmError("model did not return JSON") from None
        try:
            value = json.loads(cleaned[start : end + 1])
        except ValueError as exc:
            raise LlmError("model returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise LlmError("model JSON was not an object")
    return value


@dataclass(frozen=True)
class _Shape:
    """Which optional request parameters the provider accepts."""

    tools: bool = True
    completion_key: str = "max_tokens"
    temperature: bool = True


MIN_OUTPUT_LIMIT = 256  # a "limit" below this in an error message is not an output-token limit
# "max_tokens: 1000000 > 128000, which is the maximum ..." (Anthropic and compatible proxies)
_LIMIT_COMPARE = re.compile(r"max_(?:completion_)?tokens\W+[\d,]+\s*>\s*(\d[\d,]*)", re.IGNORECASE)
# "... supports at most 16384 completion tokens", "the maximum allowed output is 8192 tokens"
_LIMIT_WORDS = re.compile(
    r"(?:at most|maximum|supports|limit(?:ed)?(?: to| of| is)?|allowed)[^0-9]{0,80}(\d[\d,]*)",
    re.IGNORECASE,
)

TOOL_WORDS = ("tool", "function")
TOKEN_WORDS = ("max_tokens", "max_completion_tokens")
MAX_ATTEMPTS = 4


def stated_output_limit(error_text: str, requested: int) -> int | None:
    """The output-token limit a provider names in its error, if it is lower than ``requested``.

    ``None`` unless the message is clearly about tokens and names a plausible lower number, so an
    unrelated error can never make the gateway retry with an invented limit.
    """
    if "token" not in error_text.lower():
        return None
    for pattern in (_LIMIT_COMPARE, _LIMIT_WORDS):
        for match in pattern.finditer(error_text):
            limit = int(match.group(1).replace(",", ""))
            if MIN_OUTPUT_LIMIT <= limit < requested:
                return limit
    return None


def _next_shape(shape: _Shape, error_text: str) -> _Shape | None:
    """Relax one unsupported parameter based on the provider's error, or None if not applicable."""
    text = error_text.lower()
    if shape.tools and any(word in text for word in TOOL_WORDS):
        return replace(shape, tools=False)  # plain-JSON fallback, the prompt asks for JSON
    if shape.completion_key == "max_tokens" and any(word in text for word in TOKEN_WORDS):
        return replace(shape, completion_key="max_completion_tokens", temperature=False)
    if shape.temperature and "temperature" in text:
        return replace(shape, temperature=False)
    return None


class LlmClient:
    def __init__(self, settings: Settings, client: OpenAI | None = None):
        if not settings.llm_configured:
            raise LlmError("LLM is not configured: set base URL, model and API key in AI Settings")
        self.settings = settings
        self.last_model = settings.llm_model  # the model that produced the latest answer
        self._output_caps: dict[str, int] = {}  # per model: limit the provider told us about
        self._client = client or OpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=REQUEST_TIMEOUT,
            max_retries=2,
        )

    def _guard_endpoint(self) -> None:
        """Refuse a request when the endpoint now points at a link-local / metadata address.

        ``llm_base_url`` is only checked (without a lookup) when it is loaded, so a host name
        that later resolves to one is caught here, right before each request.
        """
        try:
            inspect_http_url(self.settings.llm_base_url, "llm_base_url")
        except ValueError as exc:
            raise LlmError(f"LLM request refused: {exc}") from None

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> LlmClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _complete(
        self,
        messages: list[dict[str, str]],
        tool: dict[str, Any] | None,
        max_tokens: int,
        use_temperature: bool,
        model: str | None = None,
    ) -> Any:
        """One chat completion, relaxing tool/token/temperature parameters the provider rejects."""
        shape = _Shape(tools=tool is not None, temperature=use_temperature)
        name = model or self.settings.llm_model
        max_tokens = min(max_tokens, self._output_caps.get(name, max_tokens))
        for _ in range(MAX_ATTEMPTS):
            kwargs: dict[str, Any] = {
                "model": name,
                "messages": messages,
                shape.completion_key: max_tokens,
            }
            if shape.tools and tool is not None:
                kwargs["tools"] = [tool]
                kwargs["tool_choice"] = {"type": "function", "function": {"name": TOOL_NAME}}
            if shape.temperature:
                kwargs["temperature"] = self.settings.temperature
            self._guard_endpoint()
            try:
                return self._client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 - surface provider errors uniformly
                if getattr(exc, "status_code", None) in AUTH_STATUSES:
                    raise LlmAuthError(f"LLM request failed: {exc}") from exc
                limit = stated_output_limit(str(exc), max_tokens)
                if limit is not None:  # before _next_shape: the message also says "max_tokens"
                    log.info(
                        "model %s allows at most %d output tokens; lowering the request",
                        name,
                        limit,
                    )
                    self._output_caps[name] = max_tokens = limit
                    continue
                relaxed = _next_shape(shape, str(exc))
                if relaxed is None:
                    raise LlmError(f"LLM request failed: {exc}") from exc
                shape = relaxed
        raise LlmError("LLM request failed: provider rejected every request variant")

    def call_tool(
        self, system: str, user: str, tool: dict[str, Any]
    ) -> tuple[dict[str, Any], Usage]:
        """Ask for structured output, walking the fallback chain until some model delivers."""
        chain = self.settings.model_chain
        failures: list[str] = []
        wasted = Usage()  # tokens of attempts that were discarded but still cost money
        for model in chain:
            spent: list[Usage] = []
            try:
                args, usage = self._call_model(model, system, user, tool, spent)
            except LlmAuthError:
                raise  # the key is wrong for every model: do not repeat the call down the chain
            except LlmError as exc:
                wasted = sum(spent, wasted)
                if len(chain) == 1:
                    raise
                log.warning("model %s failed (%s); trying the next one", model, exc)
                failures.append(f"{model}: {exc}")
                continue
            self.last_model = model
            return args, usage + wasted
        raise LlmError("every model in the fallback chain failed: " + "; ".join(failures))

    def _call_model(
        self,
        model: str,
        system: str,
        user: str,
        tool: dict[str, Any],
        spent: list[Usage] | None = None,
    ) -> tuple[dict[str, Any], Usage]:
        """Works with tool-calling and plain-JSON-only models.

        ``spent`` receives the usage of an answer that arrived but could not be used.
        """
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        response = self._complete(messages, tool, self.settings.max_output_tokens, True, model)
        raw = ""
        try:
            choice = response.choices[0]
            self._check_finish(getattr(choice, "finish_reason", None))
            message = choice.message
            calls = getattr(message, "tool_calls", None)
            raw = calls[0].function.arguments if calls else (message.content or "")
            return parse_json_object(raw), self._usage(response, system + user, raw)
        except (TypeError, IndexError, AttributeError, KeyError) as exc:
            # e.g. an HTTP 200 whose body is an error object: choices is None
            self._note_spent(spent, response, system + user, raw)
            raise LlmError("the model returned an unusable response") from exc
        except LlmError:
            self._note_spent(spent, response, system + user, raw)
            raise

    def _note_spent(
        self, spent: list[Usage] | None, response: Any, prompt: str, completion: str
    ) -> None:
        if spent is not None:
            spent.append(self._usage(response, prompt, completion))

    def _check_finish(self, reason: str | None) -> None:
        """A filtered or cut-off reply is never usable; say why instead of 'not JSON'."""
        if reason == "content_filter":
            raise LlmError(
                "the model's safety filter blocked this finding; ask an administrator to "
                "choose another model in AI Settings"
            )
        if reason == "length":
            raise LlmError(
                "the model's answer was cut off; ask an administrator to raise "
                "max_output_tokens in AI Settings"
            )

    @staticmethod
    def _usage(response: Any, prompt: str, completion: str) -> Usage:
        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        return Usage(
            int(prompt_tokens) if prompt_tokens is not None else estimate_tokens(prompt),
            int(completion_tokens)
            if completion_tokens is not None
            else estimate_tokens(completion),
        )

    def list_models(self) -> list[str]:
        self._guard_endpoint()
        try:
            return sorted(model.id for model in self._client.models.list().data)
        except Exception as exc:  # noqa: BLE001
            raise LlmError(f"could not list models: {exc}") from exc

    def ping(self) -> str:
        messages = [{"role": "user", "content": "Reply with the single word: ok"}]
        response = self._complete(messages, None, 8, False)
        return (response.choices[0].message.content or "").strip()
