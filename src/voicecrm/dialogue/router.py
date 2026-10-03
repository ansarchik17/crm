"""HTTP API of the dialogue module: text sessions, file STT and OpenAI Realtime signaling."""

import asyncio
from time import perf_counter
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from voicecrm.config import Settings
from voicecrm.dialogue.realtime import MAX_SDP_BYTES, RealtimeError
from voicecrm.dialogue.sessions import MAX_MESSAGES, SessionStore
from voicecrm.dialogue.stt import MAX_AUDIO_BYTES, validate_wav
from voicecrm.errors import ProviderError


class Turn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Text must not be blank")
        return value.strip()


def build_router(settings: Settings, sessions: SessionStore) -> APIRouter:
    router = APIRouter()
    realtime_setup_lock = asyncio.Lock()

    @router.get("/realtime/config")
    async def realtime_config():
        return {"configured": bool(settings.openai_api_key.get_secret_value()),
                "model": settings.openai_realtime_model, "voice": settings.openai_realtime_voice,
                "test_duration_seconds": 300}

    @router.post("/realtime/calls")
    async def realtime_call(request: Request):
        # Local prototype: disallow remote Host/Origin and cross-site browser requests.
        if request.url.hostname not in ("127.0.0.1", "localhost", "::1", "testserver"):
            raise HTTPException(403, "Realtime доступен только локально.")
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Недопустимый источник запроса.")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Недопустимый источник запроса.")
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/sdp":
            raise HTTPException(415, "Используйте Content-Type: application/sdp")
        if realtime_setup_lock.locked():
            raise HTTPException(429, "Подключение уже выполняется. Повторите через несколько секунд.")
        async with realtime_setup_lock:
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_SDP_BYTES:
                    raise HTTPException(413, "Слишком большой запрос подключения.")
                body.extend(chunk)
            try:
                sdp = body.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(422, "Некорректный SDP.") from None
            if not sdp.startswith("v=0") or "m=audio " not in sdp:
                raise HTTPException(422, "Нужен SDP с аудиоканалом.")
            try:
                answer = await request.app.state.realtime.create_call(sdp)
            except RealtimeError as error:
                raise HTTPException(error.status, str(error)) from None
            return Response(answer, media_type="application/sdp", headers={"Cache-Control": "no-store"})

    @router.post("/transcriptions", openapi_extra={"requestBody": {
        "required": True, "content": {"audio/wav": {"schema": {"type": "string", "format": "binary"}}}
    }})
    async def transcribe(request: Request):
        recognizer = request.app.state.recognizer
        if recognizer is None:
            raise HTTPException(503, "STT выключен. Настройте STT_API_KEY и STT_ENABLED.")
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "audio/wav":
            raise HTTPException(415, "Используйте Content-Type: audio/wav")
        audio = bytearray()
        async for chunk in request.stream():
            if len(audio) + len(chunk) > MAX_AUDIO_BYTES:
                raise HTTPException(413, "Максимальный размер WAV — 4 MiB")
            audio.extend(chunk)
        payload = bytes(audio)
        try:
            validate_wav(payload)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        started = perf_counter()
        try:
            text = await recognizer.transcribe(payload)
        except ProviderError:
            raise HTTPException(502, "Ошибка STT: сервис недоступен или формат ответа не подтверждён.") from None
        return {"text": text, "speech_detected": bool(text),
                "latency_ms": round((perf_counter() - started) * 1000)}

    @router.post("/sessions", status_code=201)
    async def create_session():
        session_id = sessions.create()
        if session_id is None:
            raise HTTPException(429, "Session limit reached; delete unused sessions")
        return {"session_id": session_id, "mode": settings.llm_mode}

    @router.post("/sessions/{session_id}/messages")
    async def send_message(session_id: UUID, turn: Turn, request: Request):
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(404, "Session not found")
        if session.lock.locked():
            raise HTTPException(409, "A reply is already being generated")
        async with session.lock:
            if len(session.messages) >= MAX_MESSAGES:
                raise HTTPException(409, "Session turn limit reached; start a new session")
            messages = [*session.messages, {"role": "user", "content": turn.text}]
            started = perf_counter()
            try:
                answer = await request.app.state.model.reply(messages)
            except ProviderError:
                raise HTTPException(503, "AI временно недоступен. Повторите запрос.") from None
            session.messages = [*messages, {"role": "assistant", "content": answer}]
            return {"session_id": session_id, "text": answer, "mode": settings.llm_mode,
                    "latency_ms": round((perf_counter() - started) * 1000)}

    @router.delete("/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: UUID):
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(404, "Session not found")
        if session.lock.locked():
            raise HTTPException(409, "A reply is already being generated")
        sessions.delete(session_id)

    return router
