import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from voicecrm.config import Settings
from voicecrm.dialogue.llm import OpenAIModel
from voicecrm.dialogue.prompt import SYSTEM_PROMPT
from voicecrm.errors import ProviderError
from voicecrm.main import create_app


def settings(**kwargs):
    return Settings(_env_file=None, llm_mode="openai", stt_enabled=False, **kwargs)


def completion(content="Здравствуйте", finish_reason="stop", refusal=None):
    return {"choices": [{"finish_reason": finish_reason,
                         "message": {"role": "assistant", "content": content, "refusal": refusal}}]}


@pytest.mark.parametrize("effort", [None, "none", "low", "medium", "high", "xhigh"])
def test_openai_request_contract_and_context(effort):
    config = settings(openai_api_key="test-secret", openai_reasoning_effort=effort)
    history = [{"role": "user", "content": "Нужна чистка"},
               {"role": "assistant", "content": "В какой день?"},
               {"role": "user", "content": "В пятницу"}]

    def handle(request):
        assert str(request.url) == "https://api.openai.com/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-secret"
        payload = json.loads(request.content)
        expected = {"model": "gpt-5.4-mini",
                    "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *history],
                    "stream": False, "max_completion_tokens": 256, "store": False}
        if effort is not None:
            expected["reasoning_effort"] = effort
        assert payload == expected
        return httpx.Response(200, json=completion("  Какое время удобно?  "))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            assert await OpenAIModel(config, client).reply(history) == "Какое время удобно?"
        assert len(history) == 3

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["401", "403", "429", "500", "timeout", "invalid_json",
                                  "missing_choices", "empty_choices", "wrong_message", "missing_content",
                                  "wrong_content", "empty", "blank", "truncated", "refusal",
                                  "filtered", "missing_finish_reason"])
def test_openai_errors_are_sanitized(kind):
    config = settings(openai_api_key="test-secret")

    def handle(request):
        if kind.isdigit():
            return httpx.Response(int(kind), text="test-secret private provider detail")
        if kind == "timeout":
            raise httpx.ReadTimeout("test-secret private provider detail", request=request)
        if kind == "invalid_json":
            return httpx.Response(200, text="test-secret not JSON")
        malformed = {
            "missing_choices": {},
            "empty_choices": {"choices": []},
            "wrong_message": {"choices": [{"finish_reason": "stop", "message": None}]},
            "missing_content": {"choices": [{"finish_reason": "stop", "message": {}}]},
            "wrong_content": completion([{"text": "test-secret"}]),
            "empty": completion(""),
            "blank": completion(" \n "),
            "truncated": completion("test-secret partial reply", finish_reason="length"),
            "refusal": completion("test-secret", refusal="test-secret refusal"),
            "filtered": completion("test-secret partial reply", finish_reason="content_filter"),
            "missing_finish_reason": {"choices": [{"message": {"content": "test-secret"}}]},
        }
        return httpx.Response(200, json=malformed[kind])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ProviderError, match="^Language model unavailable$") as error:
                await OpenAIModel(config, client).reply([{"role": "user", "content": "Привет"}])
            assert "test-secret" not in str(error.value)

    asyncio.run(run())


def test_openai_whole_request_deadline_bounds_a_stalled_transport():
    config = settings(openai_api_key="test-secret", llm_timeout_seconds=0.01)

    async def handle(request):
        await asyncio.Event().wait()
        raise AssertionError("Stalled request should have been cancelled")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ProviderError, match="^Language model unavailable$"):
                await OpenAIModel(config, client).reply([{"role": "user", "content": "Привет"}])

    asyncio.run(run())


def mock_app_transport(monkeypatch, handle):
    async_client = httpx.AsyncClient
    monkeypatch.setattr("voicecrm.main.httpx.AsyncClient",
                        lambda: async_client(transport=httpx.MockTransport(handle)))


def test_openai_mode_wires_model_and_keeps_session_context(monkeypatch):
    requests = []

    def handle(request):
        assert str(request.url) == "https://api.openai.com/v1/chat/completions"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=completion("Какое время удобно?"))

    mock_app_transport(monkeypatch, handle)
    with TestClient(create_app(settings(openai_api_key="test-secret"))) as client:
        assert isinstance(client.app.state.model, OpenAIModel)
        health = client.get("/health").json()
        assert health["mode"] == "openai"
        assert health["llm_configured"] is True
        assert health["llm_model"] == "gpt-5.4-mini"
        created = client.post("/sessions")
        assert created.status_code == 201
        assert created.json()["mode"] == "openai"
        sid = created.json()["session_id"]
        for text in ("Нужна чистка", "В пятницу"):
            response = client.post(f"/sessions/{sid}/messages", json={"text": text})
            assert response.status_code == 200
            assert response.json()["mode"] == "openai"
            assert response.json()["text"] == "Какое время удобно?"
    assert len(requests) == 2
    assert requests[1]["messages"] == [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "Нужна чистка"},
        {"role": "assistant", "content": "Какое время удобно?"},
        {"role": "user", "content": "В пятницу"},
    ]


def test_openai_missing_key_keeps_pages_available_and_rejects_sessions(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        raise AssertionError("No upstream request is allowed without a key")

    mock_app_transport(monkeypatch, handle)
    with TestClient(create_app(settings(openai_api_key=""))) as client:
        for path in ("/voice", "/realtime", "/realtime/config"):
            assert client.get(path).status_code == 200
        health = client.get("/health").json()
        assert health["mode"] == "openai"
        assert health["llm_configured"] is False
        assert client.get("/realtime/config").json()["configured"] is False
        response = client.post("/sessions")
        assert response.status_code == 503
        assert "OPENAI_API_KEY" in response.json()["detail"]
    assert requests == []


def test_openai_model_missing_key_never_contacts_provider():
    def handle(request):
        raise AssertionError("No upstream request is allowed without a key")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ProviderError, match="^Language model unavailable$"):
                await OpenAIModel(settings(openai_api_key=""), client).reply(
                    [{"role": "user", "content": "Привет"}])

    asyncio.run(run())
