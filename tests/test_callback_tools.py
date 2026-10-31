import asyncio
import json

import httpx
import pytest

from voicecrm.config import Settings
from voicecrm.dialogue.callback_tools import (
    CALLBACK_DRAFT_REPLY,
    CALLBACK_REPLIES,
    CALLBACK_RESPONSE_FORMAT,
    CALLBACK_SYSTEM_PROMPT,
    CALLBACK_TOOLS,
    parse_callback_arguments,
)
from voicecrm.dialogue.llm import OpenAIModel
from voicecrm.errors import ProviderError


ARGUMENTS = {"phone": "+15550000123", "summary": "Перезвонить завтра после 15:00", "contact_id": None}
HISTORY = [{"role": "user", "content": "Перезвоните завтра после 15:00, +15550000123"}]


def settings(**kwargs):
    return Settings(_env_file=None, llm_mode="openai", stt_enabled=False, **kwargs)


def tool_completion(*, name="prepare_callback", arguments=None, call_id="call_one",
                    content=None, finish_reason="tool_calls"):
    call = {"id": call_id, "type": "function", "function": {
        "name": name,
        "arguments": json.dumps(ARGUMENTS if arguments is None else arguments),
    }}
    return {"choices": [{"finish_reason": finish_reason, "message": {
        "role": "assistant", "content": content, "tool_calls": [call],
    }}]}


def text_completion(text):
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}]}


def run_tool_model(handle, execute, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await OpenAIModel(settings(openai_api_key="test-secret", **kwargs), client).reply_with_tools(
                HISTORY, execute)
    return asyncio.run(run())


@pytest.mark.parametrize("effort", [None, "none", "low"])
def test_prepare_persists_draft_once_and_discards_model_success_claim(effort):
    requests = []
    executions = []

    def handle(request):
        assert str(request.url) == "https://api.openai.com/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-secret"
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["messages"] == [{"role": "system", "content": CALLBACK_SYSTEM_PROMPT}, *HISTORY]
        assert payload["tools"] == CALLBACK_TOOLS
        assert payload["response_format"] == CALLBACK_RESPONSE_FORMAT
        assert payload["parallel_tool_calls"] is False
        assert payload["tool_choice"] == "auto"
        assert payload["stream"] is False
        assert payload["store"] is False
        assert payload["max_completion_tokens"] == 512
        if effort is None:
            assert "reasoning_effort" not in payload
        else:
            assert payload["reasoning_effort"] == effort
        return httpx.Response(200, json=tool_completion(content="Задача успешно создана в CRM!"))

    async def execute(name, arguments):
        executions.append((name, arguments))
        return {"status": "draft", "draft_id": "draft1", **arguments}

    assert run_tool_model(handle, execute, openai_reasoning_effort=effort) == CALLBACK_DRAFT_REPLY
    assert executions == [("prepare_callback", ARGUMENTS)]
    assert len(requests) == 1
    assert HISTORY == [{"role": "user", "content": "Перезвоните завтра после 15:00, +15550000123"}]
    schema = CALLBACK_TOOLS[0]["function"]
    assert schema["strict"] is True
    assert schema["parameters"]["additionalProperties"] is False
    assert set(schema["parameters"]["required"]) == {"phone", "summary", "contact_id"}
    assert "assignee_id" not in schema["parameters"]["properties"]
    response_format = requests[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    reply_schema = response_format["json_schema"]["schema"]
    assert reply_schema["additionalProperties"] is False
    assert reply_schema["required"] == ["reply_code"]
    assert reply_schema["properties"] == {
        "reply_code": {"type": "string", "enum": list(CALLBACK_REPLIES)}}


def test_ambiguous_contacts_are_returned_as_tool_result_for_clarification():
    requests = []
    executions = []
    result = {"status": "error", "code": "ambiguous_contacts",
              "contacts": [{"id": "contact1", "name": "Клиент Один"}, {"id": "contact2", "name": "Клиент Два"}]}

    def handle(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json=tool_completion())
        messages = requests[-1]["messages"]
        assert messages[-2] == {"role": "assistant", "content": None,
                                "tool_calls": tool_completion()["choices"][0]["message"]["tool_calls"]}
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["tool_call_id"] == "call_one"
        assert json.loads(messages[-1]["content"]) == result
        assert requests[-1]["response_format"] == CALLBACK_RESPONSE_FORMAT
        return httpx.Response(200, json=text_completion(json.dumps({"reply_code": "ask_contact"})))

    async def execute(name, arguments):
        executions.append(arguments)
        return result

    assert run_tool_model(handle, execute) == CALLBACK_REPLIES["ask_contact"]
    assert len(requests) == 2
    assert executions == [ARGUMENTS]


@pytest.mark.parametrize("reply_code", list(CALLBACK_REPLIES))
@pytest.mark.parametrize("empty_calls", [False, True])
def test_plain_stop_uses_only_the_server_template_without_executing_a_tool(reply_code, empty_calls):
    def handle(request):
        payload = json.loads(request.content)
        assert payload["response_format"] == CALLBACK_RESPONSE_FORMAT
        completion = text_completion(json.dumps({"reply_code": reply_code}))
        if empty_calls:
            completion["choices"][0]["message"]["tool_calls"] = []
        return httpx.Response(200, json=completion)

    async def execute(name, arguments):
        raise AssertionError("A reply code must not execute a tool")

    assert run_tool_model(handle, execute) == CALLBACK_REPLIES[reply_code]


@pytest.mark.parametrize("content", [
    "Задача создана в CRM.",
    "Клиент записан на завтра, перевод выполнен.",
    '"Задача создана в CRM."',
    '{"reply_code":"succeeded"}',
    '{"reply_code":"callback_help","text":"Задача создана в CRM."}',
    '{"reply_code":"callback_help","status":"succeeded"}',
    '{"text":"Задача создана в CRM."}',
    '{"reply_code":"ask_phone","reply_code":"callback_help"}',
    '{"reply_code":null}',
    '{"reply_code":true}',
    '{"reply_code":["callback_help"]}',
    '{"reply_code":{"text":"Задача создана"}}',
    '{"reply_code":" CALLBACK_HELP "}',
    "{}", "[]", "null", "123", "", " ", None,
    '[{"reply_code":"callback_help"}]',
    " " * 2049,
    "[" * 1500 + "0" + "]" * 1500,
])
def test_plain_stop_rejects_false_success_and_any_content_outside_the_finite_contract(content):
    def handle(request):
        return httpx.Response(200, json=text_completion(content))

    async def execute(name, arguments):
        raise AssertionError("Invalid replies must not execute a tool")

    with pytest.raises(ProviderError, match="^Language model unavailable$"):
        run_tool_model(handle, execute)


def test_structured_reply_refusal_is_sanitized_even_with_an_allowed_code():
    def handle(request):
        completion = text_completion('{"reply_code":"callback_help"}')
        completion["choices"][0]["message"]["refusal"] = "test-secret private provider detail"
        return httpx.Response(200, json=completion)

    async def execute(name, arguments):
        raise AssertionError("A refusal must not execute a tool")

    with pytest.raises(ProviderError, match="^Language model unavailable$"):
        run_tool_model(handle, execute)


def test_followup_tool_can_correct_validation_error_but_stops_after_draft():
    requests = []
    executions = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=tool_completion(call_id=f"call_{len(requests)}"))

    async def execute(name, arguments):
        executions.append(arguments)
        if len(executions) == 1:
            return {"status": "error", "code": "contact_not_found", "contacts": []}
        return {"status": "draft", "draft_id": "draft1", **arguments}

    assert run_tool_model(handle, execute) == CALLBACK_DRAFT_REPLY
    assert len(executions) == 2
    assert len(requests) == 2


@pytest.mark.parametrize("invalid", [
    {**ARGUMENTS, "assignee_id": "admin"},
    {**ARGUMENTS, "confirmed": True},
    {**ARGUMENTS, "phone": "+0123456789"},
    {**ARGUMENTS, "phone": "+1 (555) 000-0123"},
    {**ARGUMENTS, "phone": 15550000123},
    {**ARGUMENTS, "summary": "  \n  "},
    {**ARGUMENTS, "summary": "a" * 2001},
    {**ARGUMENTS, "summary": "text\x00private"},
    {**ARGUMENTS, "contact_id": "../admin"},
    {"phone": ARGUMENTS["phone"], "summary": ARGUMENTS["summary"]},
    [ARGUMENTS],
])
def test_invalid_tool_arguments_never_reach_executor(invalid):
    def handle(request):
        return httpx.Response(200, json=tool_completion(arguments=invalid))

    async def execute(name, arguments):
        raise AssertionError("Invalid tool arguments must not execute")

    with pytest.raises(ProviderError, match="^Language model unavailable$"):
        run_tool_model(handle, execute)


@pytest.mark.parametrize("kind", ["unknown", "invalid_json", "duplicate_keys", "oversized_arguments", "deep_json",
                                  "multiple_calls", "empty_calls", "wrong_type", "invalid_id", "refusal",
                                  "truncated", "stop_with_tool", "missing_choices", "wrong_message",
                                  "wrong_tool", "missing_function", "http_error", "timeout"])
def test_malformed_responses_are_sanitized_before_tool_execution(kind):
    def handle(request):
        if kind == "http_error":
            return httpx.Response(500, text="test-secret private provider detail")
        if kind == "timeout":
            raise httpx.ReadTimeout("test-secret private provider detail", request=request)
        response = tool_completion()
        choice = response["choices"][0]
        call = choice["message"]["tool_calls"][0]
        if kind == "unknown":
            call["function"]["name"] = "create_task"
        elif kind == "invalid_json":
            call["function"]["arguments"] = "test-secret not JSON"
        elif kind == "duplicate_keys":
            call["function"]["arguments"] = '{"phone":"+15550000123","phone":"+15550000124"}'
        elif kind == "oversized_arguments":
            call["function"]["arguments"] = " " * 12001
        elif kind == "deep_json":
            call["function"]["arguments"] = "[" * 1500 + "0" + "]" * 1500
        elif kind == "multiple_calls":
            choice["message"]["tool_calls"].append(call)
        elif kind == "empty_calls":
            choice["message"]["tool_calls"] = []
        elif kind == "wrong_type":
            call["type"] = "custom"
        elif kind == "invalid_id":
            call["id"] = None
        elif kind == "refusal":
            choice["message"]["refusal"] = "test-secret refusal"
        elif kind == "truncated":
            choice["finish_reason"] = "length"
        elif kind == "stop_with_tool":
            choice["finish_reason"] = "stop"
        elif kind == "missing_choices":
            response = {}
        elif kind == "wrong_message":
            choice["message"] = None
        elif kind == "wrong_tool":
            choice["message"]["tool_calls"] = [None]
        elif kind == "missing_function":
            del call["function"]
        return httpx.Response(200, json=response)

    async def execute(name, arguments):
        raise AssertionError("Invalid tool calls must not execute")

    with pytest.raises(ProviderError, match="^Language model unavailable$") as error:
        run_tool_model(handle, execute)
    assert "test-secret" not in str(error.value)


def test_missing_key_never_calls_provider_or_executor():
    def handle(request):
        raise AssertionError("No upstream request without key")

    async def execute(name, arguments):
        raise AssertionError("No tool without key")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ProviderError, match="^Language model unavailable$"):
                await OpenAIModel(settings(openai_api_key=""), client).reply_with_tools(HISTORY, execute)
    asyncio.run(run())


def test_repeated_tool_call_id_is_rejected_without_second_execution():
    executions = []

    def handle(request):
        return httpx.Response(200, json=tool_completion())

    async def execute(name, arguments):
        executions.append(arguments)
        return {"status": "error", "code": "ambiguous_contacts", "contacts": []}

    with pytest.raises(ProviderError, match="^Language model unavailable$"):
        run_tool_model(handle, execute)
    assert len(executions) == 1


def test_tool_rounds_are_bounded():
    requests = []
    executions = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=tool_completion(call_id=f"call_{len(requests)}"))

    async def execute(name, arguments):
        executions.append(arguments)
        return {"status": "error", "code": "ambiguous_contacts", "contacts": []}

    with pytest.raises(ProviderError, match="^Language model unavailable$"):
        run_tool_model(handle, execute)
    assert len(requests) == 3
    assert len(executions) == 3


@pytest.mark.parametrize("result", [None, "test-secret", {"status": "created"}, {"status": "confirmed"},
                                   {"status": "draft"}, {"status": "draft", "draft_id": " "}])
def test_executor_cannot_report_model_confirmation(result):
    def handle(request):
        return httpx.Response(200, json=tool_completion())

    async def execute(name, arguments):
        return result

    with pytest.raises(ProviderError, match="^Language model unavailable$"):
        run_tool_model(handle, execute)


def test_executor_exception_is_sanitized():
    def handle(request):
        return httpx.Response(200, json=tool_completion())

    async def execute(name, arguments):
        raise RuntimeError("test-secret private executor detail")

    with pytest.raises(ProviderError, match="^Language model unavailable$"):
        run_tool_model(handle, execute)


def test_tool_model_whole_reply_deadline_bounds_stalled_provider():
    async def handle(request):
        await asyncio.Event().wait()
        raise AssertionError("Must cancel stalled transport")

    async def execute(name, arguments):
        raise AssertionError("No result from stalled transport")

    with pytest.raises(ProviderError, match="^Language model unavailable$"):
        run_tool_model(handle, execute, llm_timeout_seconds=0.01)


def test_callback_arguments_keep_last_wishes_and_trim_edges():
    arguments = {**ARGUMENTS, "summary": "  После 17:00 вместо 15:00, чистка  ", "contact_id": "contact1"}
    assert parse_callback_arguments(json.dumps(arguments)) == {
        **arguments, "summary": "После 17:00 вместо 15:00, чистка"}
