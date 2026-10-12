"""OpenAI-compatible chat client (OpenAI, OpenRouter, Ollama, LM Studio, vLLM, ...)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any

from openai import OpenAI

from .config import Settings
from .prompts import TOOL_NAME

REQUEST_TIMEOUT = 180.0
CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class LlmError(RuntimeError):
    pass


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


TOOL_WORDS = ("tool", "function")
TOKEN_WORDS = ("max_tokens", "max_completion_tokens")
MAX_ATTEMPTS = 4


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
        self._client = client or OpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=REQUEST_TIMEOUT,
            max_retries=2,
        )

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
    ) -> Any:
        """One chat completion, relaxing tool/token/temperature parameters the provider rejects."""
        shape = _Shape(tools=tool is not None, temperature=use_temperature)
        for _ in range(MAX_ATTEMPTS):
            kwargs: dict[str, Any] = {
                "model": self.settings.llm_model,
                "messages": messages,
                shape.completion_key: max_tokens,
            }
            if shape.tools and tool is not None:
                kwargs["tools"] = [tool]
                kwargs["tool_choice"] = {"type": "function", "function": {"name": TOOL_NAME}}
            if shape.temperature:
                kwargs["temperature"] = self.settings.temperature
            try:
                return self._client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 - surface provider errors uniformly
                relaxed = _next_shape(shape, str(exc))
                if relaxed is None:
                    raise LlmError(f"LLM request failed: {exc}") from exc
                shape = relaxed
        raise LlmError("LLM request failed: provider rejected every request variant")

    def call_tool(
        self, system: str, user: str, tool: dict[str, Any]
    ) -> tuple[dict[str, Any], Usage]:
        """Ask for structured output; works with tool-calling and plain-JSON-only models."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        response = self._complete(messages, tool, self.settings.max_output_tokens, True)
        message = response.choices[0].message
        calls = getattr(message, "tool_calls", None)
        raw = calls[0].function.arguments if calls else (message.content or "")
        return parse_json_object(raw), self._usage(response, system + user, raw)

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
        try:
            return sorted(model.id for model in self._client.models.list().data)
        except Exception as exc:  # noqa: BLE001
            raise LlmError(f"could not list models: {exc}") from exc

    def ping(self) -> str:
        messages = [{"role": "user", "content": "Reply with the single word: ok"}]
        response = self._complete(messages, None, 8, False)
        return (response.choices[0].message.content or "").strip()
