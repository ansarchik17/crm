import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from voicecrm.config import Settings
from voicecrm.main import create_app
from voicecrm.errors import ProviderError


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


def test_voice_page_and_assets_are_available_without_exposing_configuration():
    with TestClient(create_app(settings(llm_mode="demo"))) as client:
        response = client.get("/voice")
        assert response.status_code == 200
        assert "Тестовая стоматология" in response.text
        for asset in ("app.js", "wav.js", "recorder.js", "style.css"):
            assert client.get(f"/static/{asset}").status_code == 200
        assert client.get("/static/.env").status_code == 404
        assert client.get("/health").json()["tts_connected"] is False


def test_primary_page_redirects_to_espocrm_and_health_does_not_claim_live_connection():
    with TestClient(create_app(settings(espocrm_api_key="test-secret"))) as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "http://127.0.0.1:8080"
        health = client.get("/health").json()
        assert health["crm_provider"] == "espocrm"
        assert health["crm_configured"] is True
        assert health["crm_connected"] is None
        assert "test-secret" not in str(health)


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


def test_removed_provider_mode_is_rejected():
    with pytest.raises(ValidationError) as error:
        settings(llm_mode="qwen")
    assert error.value.errors()[0]["loc"] == ("llm_mode",)
