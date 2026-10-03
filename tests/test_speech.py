import asyncio
from email.parser import BytesParser
from email.policy import default
import io
import wave

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from voicecrm.config import Settings
from voicecrm.main import create_app
from voicecrm.errors import ProviderError
from voicecrm.dialogue.stt import AlemSpeechRecognizer, MAX_AUDIO_BYTES, validate_wav


def wav_data(seconds=1, channels=1):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000 * seconds * channels)
    return buffer.getvalue()


class FakeRecognizer:
    def __init__(self):
        self.calls = 0
        self.fail = False

    async def transcribe(self, audio):
        self.calls += 1
        if self.fail:
            raise ProviderError("private upstream detail")
        return "Хочу оставить заявку"


def test_audio_upload_and_invalid_inputs_do_not_reach_provider():
    recognizer = FakeRecognizer()
    with TestClient(create_app(Settings(_env_file=None), recognizer=recognizer)) as client:
        response = client.post("/transcriptions", content=wav_data(), headers={"Content-Type": "audio/wav"})
        assert response.status_code == 200
        assert response.json()["text"] == "Хочу оставить заявку"
        for audio, mime, expected in [
            (b"invalid", "audio/wav", 422),
            (wav_data(), "audio/mpeg", 415),
            (wav_data(seconds=31), "audio/wav", 422),
            (wav_data(channels=2), "audio/wav", 422),
            (b"x" * (MAX_AUDIO_BYTES + 1), "audio/wav", 413),
        ]:
            assert client.post("/transcriptions", content=audio, headers={"Content-Type": mime}).status_code == expected
        assert recognizer.calls == 1
        recognizer.fail = True
        response = client.post("/transcriptions", content=wav_data(), headers={"Content-Type": "audio/wav"})
        assert response.status_code == 502
        assert "private" not in response.text


def test_stt_disabled_and_configuration_requires_key():
    with TestClient(create_app(Settings(_env_file=None, stt_enabled=False))) as client:
        assert client.post("/transcriptions", content=b"audio").status_code == 503
    with pytest.raises(ValidationError):
        Settings(_env_file=None, stt_enabled=True, stt_api_key="")


def test_truncated_wav_is_rejected():
    with pytest.raises(ValueError):
        validate_wav(wav_data()[:-10])


@pytest.mark.parametrize("model,language", [("speech-to-text", "en"), ("speech-to-text-kk", "")])
def test_alem_multipart_contract(model, language):
    audio = wav_data()
    config = Settings(_env_file=None, stt_enabled=True, stt_api_key="test-secret",
                      stt_model=model, stt_language=language)

    def handle(request):
        assert str(request.url) == "https://llm.alem.ai/v1/audio/transcriptions"
        assert request.headers["Authorization"] == "Bearer test-secret"
        envelope = b"Content-Type: " + request.headers["Content-Type"].encode() + b"\r\n\r\n" + request.content
        parts = {part.get_param("name", header="content-disposition"): part
                 for part in BytesParser(policy=default).parsebytes(envelope).iter_parts()}
        assert parts["model"].get_payload(decode=True).decode() == model
        assert parts["file"].get_payload(decode=True) == audio
        assert parts["file"].get_filename() == "speech.wav"
        assert ("language" in parts) == bool(language)
        if language:
            assert parts["language"].get_payload(decode=True).decode() == language
        return httpx.Response(200, json={"text": "  Сәлем  "})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            assert await AlemSpeechRecognizer(config, client).transcribe(audio) == "Сәлем"
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["401", "429", "500", "timeout", "invalid_json", "missing_text", "wrong_type"])
def test_stt_provider_errors_are_sanitized(failure):
    def handle(request):
        if failure.isdigit():
            return httpx.Response(int(failure), text="private upstream detail")
        if failure == "timeout":
            raise httpx.ReadTimeout("private", request=request)
        if failure == "invalid_json":
            return httpx.Response(200, text="not json")
        return httpx.Response(200, json={} if failure == "missing_text" else {"text": None})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ProviderError, match="Speech recognition unavailable"):
                await AlemSpeechRecognizer(Settings(_env_file=None), client).transcribe(wav_data())
    asyncio.run(run())
