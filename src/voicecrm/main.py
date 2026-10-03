"""Application entry point: wires the modules together and serves the local test pages.

Module map (see docs/ROADMAP.md for status and stages):
  voicecrm.dialogue   STT, language model, prompt, sessions, OpenAI Realtime  (prototype works)
  voicecrm.telephony  Asterisk/SIP, extensions, queues, transfers              (not started)
  voicecrm.crm        CRM integration with idempotent event delivery          (not started)
  voicecrm.analytics  call recordings, post-call transcription and analysis   (not started)
"""

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from voicecrm.config import Settings
from voicecrm.dialogue.llm import DemoModel, LanguageModel, QwenModel
from voicecrm.dialogue.realtime import RealtimeGateway
from voicecrm.dialogue.router import build_router
from voicecrm.dialogue.sessions import SessionStore
from voicecrm.dialogue.stt import AlemSpeechRecognizer, SpeechRecognizer


def create_app(settings: Settings | None = None, model: LanguageModel | None = None,
               recognizer: SpeechRecognizer | None = None,
               realtime: RealtimeGateway | None = None) -> FastAPI:
    settings = settings or Settings()
    sessions = SessionStore()

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
    app.include_router(build_router(settings, sessions))

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/realtime", include_in_schema=False)
    async def realtime_page():
        return FileResponse(static_dir / "realtime.html", headers={"Cache-Control": "no-store"})

    @app.get("/health")
    async def health():
        # Reports process configuration only; it does not probe external providers.
        return {"status": "ok", "mode": settings.llm_mode, "telephony_connected": False,
                "stt_enabled": settings.stt_enabled, "tts_connected": False, "crm_connected": False}

    return app


app = create_app()
