import json
from types import SimpleNamespace

import pytest

from pwndoc_ai.config import Settings, apply_patch
from pwndoc_ai.llm import LlmClient, LlmError, Usage, estimate_tokens, parse_json_object
from pwndoc_ai.prompts import TOOL_NAME, build_tool

CONFIGURED = apply_patch(Settings(), {"llm_api_key": "k", "llm_model": "m"})
TOOL = build_tool(("title", "description"), with_custom_fields=False)


def test_parse_plain_json():
    assert parse_json_object('{"a": 1}') == {"a": 1}


def test_parse_strips_code_fence():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_surrounded_by_prose():
    assert parse_json_object('Sure! Here it is: {"a": {"b": 2}} Hope that helps.') == {
        "a": {"b": 2}
    }


@pytest.mark.parametrize("text", ["", "no json here", "{broken", "[1, 2]", '"str"'])
def test_parse_rejects_bad_output(text):
    with pytest.raises(LlmError):
        parse_json_object(text)


def test_usage_addition_and_total():
    total = Usage(1, 2) + Usage(10, 20)
    assert total == Usage(11, 22)
    assert total.total == 33


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd" * 10) == 10
    assert estimate_tokens("a") == 1


def _response(*, tool_args=None, content=None, usage=None):
    calls = None
    if tool_args is not None:
        calls = [SimpleNamespace(function=SimpleNamespace(arguments=tool_args))]
    message = SimpleNamespace(tool_calls=calls, content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class FakeOpenAI:
    def __init__(self, response=None, error=None):
        self.calls = []
        self._response = response
        self._error = error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(
            list=lambda: SimpleNamespace(data=[SimpleNamespace(id="b"), SimpleNamespace(id="a")])
        )

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return self._response


def test_unconfigured_settings_raise():
    with pytest.raises(LlmError):
        LlmClient(Settings(), client=FakeOpenAI())


def test_tool_call_path_uses_reported_usage():
    usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7)
    fake = FakeOpenAI(_response(tool_args=json.dumps({"title": "T"}), usage=usage))
    args, used = LlmClient(CONFIGURED, client=fake).call_tool("sys", "user", TOOL)
    assert args == {"title": "T"}
    assert used == Usage(11, 7)
    sent = fake.calls[0]
    assert sent["model"] == "m"
    assert sent["tool_choice"]["function"]["name"] == TOOL_NAME
    assert sent["tools"] == [TOOL]


def test_json_content_fallback_when_no_tool_call():
    fake = FakeOpenAI(_response(content='```json\n{"title": "From text"}\n```'))
    args, _ = LlmClient(CONFIGURED, client=fake).call_tool("sys", "user", TOOL)
    assert args == {"title": "From text"}


def test_usage_falls_back_to_estimate():
    fake = FakeOpenAI(_response(tool_args='{"title": "T"}', usage=None))
    _, used = LlmClient(CONFIGURED, client=fake).call_tool("s" * 40, "u" * 40, TOOL)
    assert used.input_tokens == estimate_tokens("s" * 40 + "u" * 40)
    assert used.output_tokens == estimate_tokens('{"title": "T"}')


def test_provider_error_is_wrapped():
    fake = FakeOpenAI(error=RuntimeError("401 bad key"))
    with pytest.raises(LlmError, match="401 bad key"):
        LlmClient(CONFIGURED, client=fake).call_tool("s", "u", TOOL)


def test_list_models_sorted_and_ping():
    fake = FakeOpenAI(_response(content=" ok "))
    client = LlmClient(CONFIGURED, client=fake)
    assert client.list_models() == ["a", "b"]
    assert client.ping() == "ok"


def test_build_tool_only_requested_fields():
    tool = build_tool(("title", "references", "customFields"), with_custom_fields=True)
    props = tool["function"]["parameters"]["properties"]
    assert set(props) == {"title", "references", "custom_fields"}
    no_cf = build_tool(("customFields",), with_custom_fields=False)
    assert no_cf["function"]["parameters"]["properties"] == {}


# -- provider compatibility fallbacks -----------------------------------------------------------
class ScriptedOpenAI:
    """Raises the queued errors in order, then returns ``response``."""

    def __init__(self, errors, response):
        self.calls, self._errors, self._response = [], list(errors), response
        self.closed = False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._errors:
            raise self._errors.pop(0)
        return self._response

    def close(self):
        self.closed = True


def test_retries_without_tools_when_provider_rejects_them():
    fake = ScriptedOpenAI(
        [RuntimeError("400: this model does not support tools")],
        _response(content='{"title": "plain json"}'),
    )
    args, _ = LlmClient(CONFIGURED, client=fake).call_tool("s", "u", TOOL)
    assert args == {"title": "plain json"}
    assert "tools" in fake.calls[0] and "tool_choice" in fake.calls[0]
    assert "tools" not in fake.calls[1] and "tool_choice" not in fake.calls[1]


def test_retries_with_max_completion_tokens_and_no_temperature():
    fake = ScriptedOpenAI(
        [RuntimeError("Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead.")],
        _response(tool_args='{"title": "T"}'),
    )
    LlmClient(CONFIGURED, client=fake).call_tool("s", "u", TOOL)
    assert "max_tokens" in fake.calls[0] and "temperature" in fake.calls[0]
    assert "max_tokens" not in fake.calls[1] and "temperature" not in fake.calls[1]
    assert fake.calls[1]["max_completion_tokens"] == CONFIGURED.max_output_tokens


def test_retries_without_temperature_when_only_temperature_is_rejected():
    fake = ScriptedOpenAI(
        [RuntimeError("temperature is not supported for this model")],
        _response(tool_args='{"title": "T"}'),
    )
    LlmClient(CONFIGURED, client=fake).call_tool("s", "u", TOOL)
    assert "temperature" not in fake.calls[1] and "max_tokens" in fake.calls[1]


def test_unrelated_errors_are_not_retried():
    fake = ScriptedOpenAI([RuntimeError("429 rate limited")], _response(tool_args="{}"))
    with pytest.raises(LlmError, match="429"):
        LlmClient(CONFIGURED, client=fake).call_tool("s", "u", TOOL)
    assert len(fake.calls) == 1


def test_gives_up_after_bounded_retries():
    errors = [
        RuntimeError("no tools"),
        RuntimeError("max_tokens bad"),
        RuntimeError("temperature bad"),
        RuntimeError("tools again"),
    ]
    fake = ScriptedOpenAI(errors, _response(tool_args="{}"))
    with pytest.raises(LlmError):
        LlmClient(CONFIGURED, client=fake).call_tool("s", "u", TOOL)
    assert len(fake.calls) <= 4


def test_ping_sends_no_tools_or_temperature_and_close_releases_client():
    fake = ScriptedOpenAI([], _response(content="ok"))
    with LlmClient(CONFIGURED, client=fake) as client:
        assert client.ping() == "ok"
    assert set(fake.calls[0]) == {"model", "messages", "max_tokens"}
    assert fake.closed
