"""File-based Alem STT adapter; this is not a streaming speech recognizer."""

import asyncio
import io
import wave
from typing import Protocol

import httpx

from voicecrm.config import Settings
from voicecrm.providers import ProviderError

MAX_AUDIO_BYTES = 4 * 1024 * 1024
MAX_AUDIO_SECONDS = 30


def validate_wav(audio: bytes) -> None:
    """Local prototype limits, not Alem service limits. No conversion is performed."""
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            frames = wav.getnframes()
            if (wav.getnchannels() != 1 or wav.getsampwidth() != 2
                    or wav.getframerate() not in (8000, 16000, 24000, 48000)
                    or not 0 < frames <= MAX_AUDIO_SECONDS * wav.getframerate()
                    or wav.getcomptype() != "NONE"):
                raise ValueError("Unsupported WAV")
            if len(wav.readframes(frames)) != frames * 2:
                raise ValueError("Truncated WAV")
    except (wave.Error, EOFError, ValueError):
        raise ValueError("Нужен PCM WAV: mono, 16 bit, 8/16/24/48 kHz, до 30 секунд.") from None


class SpeechRecognizer(Protocol):
    async def transcribe(self, audio: bytes) -> str: ...


class AlemSpeechRecognizer:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    async def transcribe(self, audio: bytes) -> str:
        fields = {"model": self.settings.stt_model}
        if self.settings.stt_language:
            fields["language"] = self.settings.stt_language
        try:
            async with asyncio.timeout(self.settings.stt_timeout_seconds):
                response = await self.client.post(
                    self.settings.stt_base_url.rstrip("/") + "/audio/transcriptions",
                    headers={"Authorization": "Bearer " + self.settings.stt_api_key.get_secret_value()},
                    data=fields,
                    files={"file": ("speech.wav", audio, "audio/wav")},
                    timeout=self.settings.stt_timeout_seconds,
                )
                response.raise_for_status()
                # Alem's public page omits the response schema. Confirm this
                # expected JSON shape with an authorized live request before pilot.
                text = response.json()["text"]
                if not isinstance(text, str) or len(text) > 2000:
                    raise ValueError("Unexpected transcript")
                return text.strip()
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
            raise ProviderError("Speech recognition unavailable or unexpected response") from None
