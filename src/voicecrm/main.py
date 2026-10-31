"""Voice service connected to EspoCRM, the project's primary CRM application.

Module map (see docs/ROADMAP.md for status and stages):
  voicecrm.dialogue   STT, language model, prompt, sessions, OpenAI Realtime  (prototype works)
  voicecrm.telephony  Asterisk/SIP, extensions, queues, transfers              (not started)
  voicecrm.crm        EspoCRM REST adapter; durable event delivery is pending
  voicecrm.analytics  call recordings, post-call transcription and analysis   (not started)
"""

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from voicecrm.config import Settings
from voicecrm.crm.client import EspoCRMClient
from voicecrm.crm.callbacks import CallbackService
from voicecrm.crm.callback_store import CallbackStore
from voicecrm.crm.callback_router import build_callback_router
from voicecrm.crm.router import build_crm_router
from voicecrm.dialogue.llm import DemoModel, LanguageModel, OpenAIModel
from voicecrm.dialogue.realtime import RealtimeGateway
from voicecrm.dialogue.router import build_router
from voicecrm.dialogue.sessions import SessionStore
from voicecrm.dialogue.stt import AlemSpeechRecognizer, SpeechRecognizer


def create_app(settings: Settings | None = None, model: LanguageModel | None = None,
               recognizer: SpeechRecognizer | None = None,
               realtime: RealtimeGateway | None = None,
               crm: EspoCRMClient | None = None,
               callbacks: CallbackService | None = None) -> FastAPI:
    settings = settings or Settings()
    sessions = SessionStore(idle_timeout_seconds=settings.session_idle_timeout_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient() as client:
            if model is not None:
                app.state.model = model
            elif settings.llm_mode == "demo":
                app.state.model = DemoModel()
            else:
                app.state.model = OpenAIModel(settings, client)
            app.state.recognizer = recognizer or (AlemSpeechRecognizer(settings, client) if settings.stt_enabled else None)
            app.state.realtime = realtime or RealtimeGateway(settings, client)
            app.state.crm = crm or EspoCRMClient(settings, client)
            app.state.callbacks = callbacks
            if callbacks is None and settings.callback_assignee_id and app.state.crm.configured:
                app.state.callbacks = CallbackService(app.state.crm, CallbackStore(settings.callback_store_path),
                                                      settings.callback_assignee_id)
            yield
        sessions.clear()

    app = FastAPI(title="VoiceCRM — голосовой сервис EspoCRM", lifespan=lifespan)
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.include_router(build_router(settings, sessions))
    app.include_router(build_crm_router(crm, settings))
    app.include_router(build_callback_router(sessions))

    @app.get("/", include_in_schema=False)
    async def index():
        return RedirectResponse(settings.espocrm_url, status_code=307)

    @app.get("/voice", include_in_schema=False)
    async def voice_page():
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/realtime", include_in_schema=False)
    async def realtime_page():
        return FileResponse(static_dir / "realtime.html", headers={"Cache-Control": "no-store"})

    @app.get("/health")
    async def health():
        # Reports process configuration only; it does not probe external providers.
        llm_model = settings.openai_model if settings.llm_mode == "openai" else None
        return {"status": "ok", "mode": settings.llm_mode, "telephony_connected": False,
                "llm_configured": settings.llm_mode != "openai" or bool(settings.openai_api_key.get_secret_value()),
                "llm_model": llm_model,
                "stt_enabled": settings.stt_enabled, "tts_connected": False,
                "crm_provider": "espocrm", "crm_configured": app.state.crm.configured,
                "callback_enabled": app.state.callbacks is not None,
                "crm_connected": None if app.state.crm.configured else False}

    return app


app = create_app()
