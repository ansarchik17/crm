"""A canceled edit request must retain its verified callback context.

Real SQLite contention, synthetic CRM and ASGI only; no providers or runtime data.
"""

import asyncio
import json
import sqlite3
import threading
from time import monotonic

import httpx
import pytest

from voicecrm.config import Settings
from voicecrm.crm.callback_store import CallbackStore
from voicecrm.crm.callbacks import CallbackService
from voicecrm.crm.client import RecordReceipt
from voicecrm.dialogue.callback_tools import CALLBACK_REPLIES
from voicecrm.main import create_app


PHONE = "+12025550199"
SUMMARY = "Синтетическая консультация в пятницу после 15:00"


class SyntheticCRM:
    configured = True

    def __init__(self):
        self.posts = []

    async def get_user(self, user_id):
        return RecordReceipt(id=user_id)

    async def search_contacts_by_phone(self, phone):
        return []

    async def create_task(self, task):
        self.posts.append(task)
        return RecordReceipt(id="synthetic_task")


class CapturingModel:
    def __init__(self):
        self.calls = []

    async def reply_with_tools(self, messages, execute_tool):
        self.calls.append(messages)
        return CALLBACK_REPLIES["clarify_callback"]


class ObservedCancelStore(CallbackStore):
    def __init__(self, path):
        super().__init__(path)
        self.cancel_started = threading.Event()
        self.cancel_finished = threading.Event()

    def transition(self, *args, **kwargs):
        if kwargs.get("status") != "canceled":
            return super().transition(*args, **kwargs)
        self.cancel_started.set()
        try:
            return super().transition(*args, **kwargs)
        finally:
            self.cancel_finished.set()


@pytest.mark.parametrize("editing_route", ["dialogue", "form"])
def test_request_cancellation_during_sqlite_cancel_preserves_verified_context(tmp_path, editing_route):
    async def run():
        store = ObservedCancelStore(tmp_path / "cancel_context.sqlite3")
        crm, model = SyntheticCRM(), CapturingModel()
        callbacks = CallbackService(crm, store, "synthetic_employee")
        config = Settings(_env_file=None, llm_mode="demo", stt_enabled=False,
                          callback_assignee_id="")
        app = create_app(config, model=model, crm=crm, callbacks=callbacks)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post("/sessions")
            assert response.status_code == 201
            sid = response.json()["session_id"]
            response = await client.post("/crm/callbacks", json={
                "session_id": sid, "phone": PHONE, "summary": SUMMARY,
            })
            assert response.status_code == 201
            draft = response.json()["callback"]

            blocker = sqlite3.connect(store.path, isolation_level=None)
            blocker.execute("BEGIN IMMEDIATE")
            if editing_route == "dialogue":
                edit_path, edit_payload = f"/sessions/{sid}/messages", {
                    "text": "Измените день, остальные пожелания сохраните",
                    "callback_id": draft["id"],
                }
            else:
                edit_path, edit_payload = "/crm/callbacks", {
                    "session_id": sid, "previous_id": draft["id"], "phone": PHONE,
                    "summary": "Новая попытка формы ещё не прошла проверку",
                }
            editing = asyncio.create_task(client.post(edit_path, json=edit_payload))
            try:
                assert await asyncio.wait_for(
                    asyncio.to_thread(store.cancel_started.wait, 2), timeout=3)
                editing.cancel()
                started = monotonic()
                await asyncio.sleep(0.05)
                assert monotonic() - started < 1, "SQLite must not block the event loop"
                assert not editing.done(), "The local cancellation must finish before the session is released"
                assert model.calls == []

                overlapping = await client.post(f"/sessions/{sid}/messages", json={
                    "text": "Поздняя реплика во время отмены",
                    "callback_id": draft["id"],
                })
                assert overlapping.status_code == 409
            finally:
                blocker.rollback()
                blocker.close()
                if not editing.done():
                    # Do not cancel twice: let the bounded local transaction drain.
                    await asyncio.wait_for(asyncio.gather(editing, return_exceptions=True), timeout=3)

            with pytest.raises(asyncio.CancelledError):
                await editing
            assert store.cancel_finished.is_set()
            assert (await callbacks.get(draft["id"])).status == "canceled"

            response = await client.post(f"/sessions/{sid}/messages", json={
                "text": "Сохраните прежний телефон и остальные пожелания",
                "callback_id": draft["id"],
            })
            assert response.status_code == 200
            assert len(model.calls) == 1
            contexts = [message["content"] for message in model.calls[0]
                        if message["role"] == "system"]
            assert len(contexts) == 1
            decoder = json.JSONDecoder()
            context, _ = decoder.raw_decode(contexts[0][contexts[0].index("{"):])
            assert context["phone"] == PHONE
            assert context["summary"] == SUMMARY
            assert context["status"] == "canceled"
            assert not {"contact_selection_confirmed", "confirmed", "assigned_user_id"} & context.keys()
            assert crm.posts == []

    asyncio.run(run())
