"""OpenAI GA Realtime WebRTC signaling; audio flows directly to OpenAI."""

import asyncio
import json

import httpx

from voicecrm.config import Settings
from voicecrm.providers import SYSTEM_PROMPT

MAX_SDP_BYTES = 64 * 1024


class RealtimeError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def session_config(settings: Settings) -> dict:
    config = {
        "type": "realtime",
        "model": settings.openai_realtime_model,
        "output_modalities": ["audio"],
        "instructions": SYSTEM_PROMPT + "\nГовори по-русски, если клиент не выбрал другой язык. "
        "Это живой голосовой разговор: отвечай одним-двумя короткими предложениями, "
        "без Markdown. Если речь неразборчива, попроси повторить. Не заполняй паузы "
        "выдуманными ответами. Для простых вопросов отвечай сразу.",
        "max_output_tokens": 1024,
        "audio": {
            "input": {
                "noise_reduction": {"type": "near_field"},
                "turn_detection": {"type": "server_vad", "threshold": 0.5,
                                   "prefix_padding_ms": 300, "silence_duration_ms": 500,
                                   "create_response": True, "interrupt_response": True},
                # Only for on-screen captions; the voice model consumes audio directly.
                "transcription": {"model": "gpt-transcribe", "language": "ru"},
            },
            "output": {"voice": settings.openai_realtime_voice},
        },
    }
    if settings.openai_realtime_model.startswith("gpt-realtime-2") and settings.openai_realtime_reasoning:
        config["reasoning"] = {"effort": settings.openai_realtime_reasoning}
    return config


class RealtimeGateway:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    async def create_call(self, sdp: str) -> str:
        if not self.settings.openai_api_key.get_secret_value():
            raise RealtimeError(503, "Добавьте OPENAI_API_KEY в локальный .env и перезапустите сервер.")
        try:
            async with asyncio.timeout(self.settings.openai_realtime_timeout_seconds):
                response = await self.client.post(
                    "https://api.openai.com/v1/realtime/calls",
                    headers={"Authorization": "Bearer " + self.settings.openai_api_key.get_secret_value()},
                    files={"sdp": (None, sdp, "application/sdp"),
                           "session": (None, json.dumps(session_config(self.settings), ensure_ascii=False), "application/json")},
                    timeout=self.settings.openai_realtime_timeout_seconds,
                )
                if response.status_code in (401, 403):
                    raise RealtimeError(502, "OpenAI отклонил доступ. Проверьте API-ключ и разрешение на Realtime.")
                if response.status_code == 429:
                    raise RealtimeError(503, "OpenAI: проверьте баланс API, квоту и ограничения частоты запросов.")
                if response.status_code in (400, 404):
                    raise RealtimeError(502, "OpenAI не принял модель или параметры Realtime. Проверьте конфигурацию.")
                response.raise_for_status()
                answer = response.text
                if not answer.startswith("v=0") or "m=audio " not in answer or len(response.content) > MAX_SDP_BYTES:
                    raise ValueError("Unexpected SDP answer")
                return answer
        except (httpx.TimeoutException, TimeoutError):
            raise RealtimeError(504, "OpenAI не ответил вовремя. Попробуйте подключиться ещё раз.") from None
        except (httpx.HTTPError, ValueError):
            raise RealtimeError(502, "Не удалось установить соединение с OpenAI Realtime.") from None
