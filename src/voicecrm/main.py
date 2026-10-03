import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from time import perf_counter
from uuid import UUID, uuid4
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from voicecrm.config import Settings
from voicecrm.providers import DemoModel, LanguageModel, ProviderError, QwenModel
from voicecrm.speech import AlemSpeechRecognizer, MAX_AUDIO_BYTES, SpeechRecognizer, validate_wav
from voicecrm.realtime import MAX_SDP_BYTES, RealtimeError, RealtimeGateway


@dataclass
class Session:
    messages: list[dict[str, str]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Turn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Text must not be blank")
        return value.strip()


def create_app(settings: Settings | None = None, model: LanguageModel | None = None,
               recognizer: SpeechRecognizer | None = None,
               realtime: RealtimeGateway | None = None) -> FastAPI:
    settings = settings or Settings()
    sessions: dict[UUID, Session] = {}
    realtime_setup_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient() as client:
            app.state.model = model or (DemoModel() if settings.llm_mode == "demo" else QwenModel(settings, client))
            app.state.recognizer = recognizer or (AlemSpeechRecognizer(settings, client) if settings.stt_enabled else None)
            app.state.realtime = realtime or RealtimeGateway(settings, client)
            yield
        sessions.clear()

    app = FastAPI(title="VoiceCRM — локальный прототип", lifespan=lifespan)
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/realtime", include_in_schema=False)
    async def realtime_page():
        return FileResponse(static_dir / "realtime.html", headers={"Cache-Control": "no-store"})

    @app.get("/realtime/config")
    async def realtime_config():
        return {"configured": bool(settings.openai_api_key.get_secret_value()),
                "model": settings.openai_realtime_model, "voice": settings.openai_realtime_voice,
                "test_duration_seconds": 300}

    @app.post("/realtime/calls")
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
                answer = await app.state.realtime.create_call(sdp)
            except RealtimeError as error:
                raise HTTPException(error.status, str(error)) from None
            return Response(answer, media_type="application/sdp", headers={"Cache-Control": "no-store"})

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": settings.llm_mode, "telephony_connected": False,
                "stt_enabled": settings.stt_enabled, "tts_connected": False, "crm_connected": False}

    @app.post("/transcriptions", openapi_extra={"requestBody": {
        "required": True, "content": {"audio/wav": {"schema": {"type": "string", "format": "binary"}}}
    }})
    async def transcribe(request: Request):
        if app.state.recognizer is None:
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
            text = await app.state.recognizer.transcribe(payload)
        except ProviderError:
            raise HTTPException(502, "Ошибка STT: сервис недоступен или формат ответа не подтверждён.") from None
        return {"text": text, "speech_detected": bool(text),
                "latency_ms": round((perf_counter() - started) * 1000)}

    @app.post("/sessions", status_code=201)
    async def create_session():
        if len(sessions) >= 100:
            raise HTTPException(429, "Session limit reached; delete unused sessions")
        session_id = uuid4()
        sessions[session_id] = Session()
        return {"session_id": session_id, "mode": settings.llm_mode}

    @app.post("/sessions/{session_id}/messages")
    async def send_message(session_id: UUID, turn: Turn):
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(404, "Session not found")
        if session.lock.locked():
            raise HTTPException(409, "A reply is already being generated")
        async with session.lock:
            if len(session.messages) >= 40:
                raise HTTPException(409, "Session turn limit reached; start a new session")
            messages = [*session.messages, {"role": "user", "content": turn.text}]
            started = perf_counter()
            try:
                answer = await app.state.model.reply(messages)
            except ProviderError:
                raise HTTPException(503, "AI временно недоступен. Повторите запрос.") from None
            session.messages = [*messages, {"role": "assistant", "content": answer}]
            return {"session_id": session_id, "text": answer, "mode": settings.llm_mode,
                    "latency_ms": round((perf_counter() - started) * 1000)}

    @app.delete("/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: UUID):
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(404, "Session not found")
        if session.lock.locked():
            raise HTTPException(409, "A reply is already being generated")
        del sessions[session_id]

    return app


app = create_app()
