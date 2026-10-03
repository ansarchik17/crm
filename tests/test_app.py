import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from voicecrm.config import Settings
from voicecrm.main import create_app
from voicecrm.dialogue.llm import QwenModel
from voicecrm.errors import ProviderError


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


def test_voice_page_and_assets_are_available_without_exposing_configuration():
    with TestClient(create_app(settings(llm_mode="demo"))) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Тестовая стоматология" in response.text
        for asset in ("app.js", "wav.js", "recorder.js", "style.css"):
            assert client.get(f"/static/{asset}").status_code == 200
        assert client.get("/static/.env").status_code == 404
        assert client.get("/health").json()["tts_connected"] is False


class RecordingModel:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def reply(self, messages):
        self.calls.append(messages.copy())
        if self.fail:
            raise ProviderError("secret provider detail")
        return "Уточните услугу."


def test_sessions_keep_separate_context_and_can_be_deleted():
    model = RecordingModel()
    with TestClient(create_app(settings(llm_mode="demo"), model)) as client:
        a, b = [client.post("/sessions").json()["session_id"] for _ in range(2)]
        assert client.post(f"/sessions/{a}/messages", json={"text": "Первый"}).status_code == 200
        client.post(f"/sessions/{a}/messages", json={"text": "Второй"})
        client.post(f"/sessions/{b}/messages", json={"text": "Другой клиент"})
        assert len(model.calls[1]) == 3
        assert model.calls[2] == [{"role": "user", "content": "Другой клиент"}]
        assert client.delete(f"/sessions/{a}").status_code == 204
        assert client.post(f"/sessions/{a}/messages", json={"text": "Повтор"}).status_code == 404


def test_failure_does_not_commit_turn_or_expose_provider_details():
    model = RecordingModel()
    with TestClient(create_app(settings(llm_mode="demo"), model)) as client:
        sid = client.post("/sessions").json()["session_id"]
        model.fail = True
        response = client.post(f"/sessions/{sid}/messages", json={"text": "Запрос"})
        assert response.status_code == 503
        assert "secret" not in response.text
        model.fail = False
        client.post(f"/sessions/{sid}/messages", json={"text": "Повтор"})
        assert len(model.calls[-1]) == 1


def test_input_and_history_are_bounded():
    with TestClient(create_app(settings(llm_mode="demo"))) as client:
        sid = client.post("/sessions").json()["session_id"]
        for text in (" ", "a" * 2001):
            assert client.post(f"/sessions/{sid}/messages", json={"text": text}).status_code == 422
        for _ in range(20):
            assert client.post(f"/sessions/{sid}/messages", json={"text": "Тест"}).status_code == 200
        assert client.post(f"/sessions/{sid}/messages", json={"text": "Ещё"}).status_code == 409
        assert client.get("/health").json()["telephony_connected"] is False


def test_qwen_requires_explicit_configuration():
    with pytest.raises(ValidationError):
        settings(llm_mode="qwen", qwen_api_key="", qwen_base_url="", qwen_model="")


@pytest.mark.parametrize("kind", ["success", "unauthorized", "malformed", "timeout"])
@pytest.mark.parametrize("thinking", [None, False, True])
def test_qwen_contract_and_failures(kind, thinking):
    config = settings(llm_mode="qwen", qwen_api_key="test-secret", qwen_model="test-model",
                      qwen_base_url="https://provider.example/v1", qwen_enable_thinking=thinking)

    def handle(request):
        assert str(request.url) == "https://provider.example/v1/chat/completions"
        payload = json.loads(request.content)
        if thinking is None:
            assert "chat_template_kwargs" not in payload
        else:
            assert payload["chat_template_kwargs"] == {"enable_thinking": thinking}
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["content"] == "Привет"
        assert request.headers["Authorization"] == "Bearer test-secret"
        if kind == "timeout":
            raise httpx.ReadTimeout("secret", request=request)
        if kind == "unauthorized":
            return httpx.Response(401, text="test-secret")
        if kind == "malformed":
            return httpx.Response(200, json={"choices": []})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Здравствуйте"}}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider = QwenModel(config, client)
            if kind == "success":
                assert await provider.reply([{"role": "user", "content": "Привет"}]) == "Здравствуйте"
            else:
                with pytest.raises(ProviderError, match="Language model unavailable"):
                    await provider.reply([{"role": "user", "content": "Привет"}])
    asyncio.run(run())
