"""Offline regressions for callback state shared by the form and dialogue."""

import json

import pytest
from fastapi.testclient import TestClient

from voicecrm.config import Settings
from voicecrm.crm.callback_store import CallbackStore
from voicecrm.crm.callbacks import CallbackService
from voicecrm.crm.client import CallbackTaskRecord, ContactSummary, RecordReceipt
from voicecrm.dialogue.callback_tools import CALLBACK_REPLIES
from voicecrm.errors import ProviderError
from voicecrm.main import create_app


PHONE = "+15550000123"
SUMMARY = "Чистка в пятницу после 15:00"
CONTACT = ContactSummary(id="synthetic_contact", name="Синтетический клиент")


class SyntheticCRM:
    configured = True

    def __init__(self, contacts=()):
        self.contacts = list(contacts)
        self.writes = []
        self.tasks = []

    async def get_user(self, user_id):
        return RecordReceipt(id=user_id)

    async def search_contacts_by_phone(self, phone):
        return self.contacts

    async def create_task(self, task):
        self.writes.append(task)
        task_id = f"synthetic_task_{len(self.writes)}"
        self.tasks.append(CallbackTaskRecord(id=task_id, **task.model_dump()))
        return RecordReceipt(id=task_id)

    async def find_callback_tasks(self, name):
        return [task for task in self.tasks if task.name == name]


class ScriptedModel:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []
        self.results = []

    async def reply_with_tools(self, messages, execute_tool):
        self.calls.append(messages)
        assert self.steps, "Unexpected model request"
        return await self.steps.pop(0)(messages, execute_tool)


def system_callback_contexts(messages):
    """Inspect structured data, without coupling to the system prompt's wording."""
    decoder = json.JSONDecoder()
    contexts = []
    required = {"phone", "summary", "contact_id", "contact_name", "status"}
    for message in messages:
        if message.get("role") != "system":
            continue
        content = message["content"]
        for position, char in enumerate(content):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(content[position:])
            except ValueError:
                continue
            if isinstance(value, dict) and required <= value.keys():
                contexts.append(value)
    return contexts


def current_context(messages, *, phone=PHONE, summary=SUMMARY, contact=None):
    contexts = system_callback_contexts(messages)
    assert len(contexts) == 1, "The model needs one current server-owned callback context"
    context = contexts[0]
    assert context["phone"] == phone
    assert context["summary"] == summary
    assert context["contact_id"] == (contact.id if contact else None)
    assert context["contact_name"] == (contact.name if contact else None)
    assert context["status"] in {"draft", "canceled"}
    assert not {"contact_selection_confirmed", "confirmed", "assigned_user_id", "assignee_id"} & context.keys()
    return context


def make_app(tmp_path, model, *, crm=None):
    crm = crm or SyntheticCRM()
    service = CallbackService(crm, CallbackStore(tmp_path / "callback_context.sqlite3"), "synthetic_employee")
    config = Settings(_env_file=None, llm_mode="demo", stt_enabled=False, callback_assignee_id="")
    return create_app(config, model=model, crm=crm, callbacks=service), crm


def new_session(client):
    response = client.post("/sessions")
    assert response.status_code == 201
    return response.json()["session_id"]


def prepare_form(client, sid=None, *, phone=PHONE, summary=SUMMARY, contact_id=None):
    response = client.post("/crm/callbacks", json={
        "session_id": sid, "phone": phone, "summary": summary, "contact_id": contact_id,
    })
    assert response.status_code == 201, response.text
    return response.json()["callback"]


def send(client, sid, text, **extra):
    return client.post(f"/sessions/{sid}/messages", json={"text": text, **extra})


def test_form_draft_keeps_phone_service_and_time_when_dialogue_changes_day(tmp_path):
    async def change_day(messages, execute_tool):
        context = current_context(messages, contact=CONTACT)
        assert messages[-1] == {"role": "user", "content": "Вместо пятницы в субботу, остальные пожелания сохраните"}
        result = await execute_tool("prepare_callback", {
            "phone": context["phone"], "summary": context["summary"].replace("пятницу", "субботу"),
            "contact_id": None,
        })
        assert result["status"] == "draft"
        return CALLBACK_REPLIES["callback_help"]

    model = ScriptedModel(change_day)
    app, crm = make_app(tmp_path, model, crm=SyntheticCRM([CONTACT]))
    with TestClient(app) as client:
        sid = new_session(client)
        old = prepare_form(client, sid)
        response = send(client, sid, "Вместо пятницы в субботу, остальные пожелания сохраните")
        assert response.status_code == 200, response.text
        updated = response.json()["callback"]
        assert updated["id"] != old["id"]
        assert updated["phone"] == PHONE
        assert updated["summary"] == "Чистка в субботу после 15:00"
        assert updated["contact_id"] == CONTACT.id
        assert client.get(f"/crm/callbacks/{old['id']}").json()["callback"]["status"] == "canceled"
        assert crm.writes == []


@pytest.mark.parametrize("first_reply", ["failure", "clarification"])
def test_canonical_form_data_survives_model_failure_or_clarification(tmp_path, first_reply):
    async def first(messages, execute_tool):
        current_context(messages)
        if first_reply == "failure":
            raise ProviderError("Synthetic provider unavailable")
        return CALLBACK_REPLIES["clarify_callback"]

    async def correct(messages, execute_tool):
        context = current_context(messages)
        result = await execute_tool("prepare_callback", {
            "phone": context["phone"], "summary": context["summary"].replace("пятницу", "субботу"),
            "contact_id": None,
        })
        assert result["status"] == "draft"
        return CALLBACK_REPLIES["callback_help"]

    app, crm = make_app(tmp_path, ScriptedModel(first, correct))
    with TestClient(app) as client:
        sid = new_session(client)
        old = prepare_form(client, sid)
        response = send(client, sid, "Нужно уточнить пожелания")
        assert response.status_code == (503 if first_reply == "failure" else 200)
        response = send(client, sid, "Вместо пятницы в субботу, остальное без изменений")
        assert response.status_code == 200, response.text
        assert response.json()["callback"]["phone"] == PHONE
        assert response.json()["callback"]["summary"] == "Чистка в субботу после 15:00"
        assert client.get(f"/crm/callbacks/{old['id']}").json()["callback"]["status"] == "canceled"
        assert crm.writes == []


def test_newer_form_overrides_older_dialogue_draft_and_history(tmp_path):
    new_phone = "+15550000999"
    new_summary = "Консультация во вторник после 17:00"

    async def older_draft(messages, execute_tool):
        await execute_tool("prepare_callback", {"phone": PHONE, "summary": SUMMARY, "contact_id": None})
        return CALLBACK_REPLIES["callback_help"]

    async def update_latest(messages, execute_tool):
        context = current_context(messages, phone=new_phone, summary=new_summary)
        history = [m["content"] for m in messages if m["role"] in {"user", "assistant"}]
        assert not any(PHONE in text or SUMMARY in text for text in history)
        result = await execute_tool("prepare_callback", {
            "phone": context["phone"], "summary": context["summary"].replace("17:00", "18:00"),
            "contact_id": None,
        })
        assert result["status"] == "draft"
        return CALLBACK_REPLIES["callback_help"]

    app, crm = make_app(tmp_path, ScriptedModel(older_draft, update_latest))
    with TestClient(app) as client:
        sid = new_session(client)
        older = send(client, sid, f"{SUMMARY}, телефон {PHONE}").json()["callback"]
        latest = prepare_form(client, sid, phone=new_phone, summary=new_summary)
        response = send(client, sid, "Лучше после 18:00, остальное без изменений")
        assert response.status_code == 200, response.text
        assert response.json()["callback"]["phone"] == new_phone
        assert response.json()["callback"]["summary"] == "Консультация во вторник после 18:00"
        for previous in (older, latest):
            assert client.get(f"/crm/callbacks/{previous['id']}").json()["callback"]["status"] == "canceled"
        assert crm.writes == []


def test_saved_draft_restored_in_new_session_supplies_verified_model_context(tmp_path):
    async def restore_and_correct(messages, execute_tool):
        context = current_context(messages, contact=CONTACT)
        result = await execute_tool("prepare_callback", {
            "phone": context["phone"], "summary": context["summary"].replace("пятницу", "субботу"),
            "contact_id": None,
        })
        assert result["status"] == "draft"
        return CALLBACK_REPLIES["callback_help"]

    app, crm = make_app(tmp_path, ScriptedModel(restore_and_correct), crm=SyntheticCRM([CONTACT]))
    with TestClient(app) as client:
        old_sid = new_session(client)
        old = prepare_form(client, old_sid)
        assert client.delete(f"/sessions/{old_sid}").status_code == 204
        sid = new_session(client)
        response = send(client, sid, "В субботу вместо пятницы", callback_id=old["id"])
        assert response.status_code == 200, response.text
        assert response.json()["callback"]["summary"] == "Чистка в субботу после 15:00"
        assert response.json()["callback"]["phone"] == PHONE
        assert crm.writes == []


@pytest.mark.parametrize("terminal_action", ["cancel", "confirm"])
@pytest.mark.parametrize("via_dialogue", [False, True])
def test_terminal_callback_data_is_not_reused_by_independent_request(tmp_path, terminal_action, via_dialogue):
    new_phone = "+15550000999"
    new_summary = "Самостоятельное новое обращение по консультации"

    async def create_first(messages, execute_tool):
        await execute_tool("prepare_callback", {"phone": PHONE, "summary": SUMMARY, "contact_id": None})
        return CALLBACK_REPLIES["callback_help"]

    async def independent(messages, execute_tool):
        assert system_callback_contexts(messages) == []
        history = [m["content"] for m in messages if m["role"] in {"user", "assistant"}]
        assert not any(PHONE in text or SUMMARY in text for text in history)
        result = await execute_tool("prepare_callback", {
            "phone": new_phone, "summary": new_summary, "contact_id": None,
        })
        assert result["status"] == "draft"
        return CALLBACK_REPLIES["callback_help"]

    app, crm = make_app(tmp_path, ScriptedModel(create_first, independent))
    with TestClient(app) as client:
        sid = new_session(client)
        old = send(client, sid, f"{SUMMARY}, {PHONE}").json()["callback"]
        if via_dialogue:
            command = "Отменяю заявку" if terminal_action == "cancel" else "Подтверждаю заявку"
            terminal = send(client, sid, command, callback_id=old["id"])
        else:
            terminal = client.post(f"/crm/callbacks/{old['id']}/{terminal_action}", json={})
        assert terminal.status_code == 200
        assert terminal.json()["callback"]["status"] == ("canceled" if terminal_action == "cancel" else "succeeded")
        response = send(client, sid, f"Новое независимое обращение: {new_summary}, {new_phone}", callback_id=old["id"])
        assert response.status_code == 200, response.text
        assert response.json()["callback"]["phone"] == new_phone
        assert response.json()["callback"]["summary"] == new_summary
        assert len(crm.writes) == (0 if terminal_action == "cancel" else 1)


@pytest.mark.parametrize("terminal_action", ["cancel", "confirm"])
@pytest.mark.parametrize("intermediate_command", [None, "Проверить результат", "Подтверждаю заявку"])
def test_repeated_terminal_id_keeps_new_phone_across_followup_turns(tmp_path, terminal_action, intermediate_command):
    new_phone = "+15550000999"

    async def ask_summary(messages, execute_tool):
        assert system_callback_contexts(messages) == []
        assert not any(PHONE in m["content"] for m in messages)
        return CALLBACK_REPLIES["ask_summary"]

    async def finish_new_request(messages, execute_tool):
        assert system_callback_contexts(messages) == []
        assert any(new_phone in m["content"] for m in messages if m["role"] == "user")
        assert not any(PHONE in m["content"] or SUMMARY in m["content"] for m in messages)
        await execute_tool("prepare_callback", {
            "phone": new_phone, "summary": "Перезвонить насчёт чистки", "contact_id": None,
        })
        return CALLBACK_REPLIES["callback_help"]

    app, crm = make_app(tmp_path, ScriptedModel(ask_summary, finish_new_request))
    with TestClient(app) as client:
        sid = new_session(client)
        old = prepare_form(client, sid)
        assert client.post(f"/crm/callbacks/{old['id']}/{terminal_action}", json={}).status_code == 200
        response = send(client, sid, f"Новое обращение. Телефон {new_phone}", callback_id=old["id"])
        assert response.status_code == 200, response.text
        retained_id = response.json()["callback"]["id"]
        if intermediate_command:
            response = send(client, sid, intermediate_command, callback_id=retained_id)
            assert response.status_code == 200, response.text
        response = send(client, sid, "Перезвонить насчёт чистки", callback_id=retained_id)
        assert response.status_code == 200, response.text
        assert response.json()["callback"]["phone"] == new_phone
        assert len(crm.writes) == (0 if terminal_action == "cancel" else 1)


@pytest.mark.parametrize("forged_permission", [False, True])
def test_model_context_never_authorizes_selection_of_ambiguous_contact(tmp_path, forged_permission):
    second_contact = ContactSummary(id="synthetic_other", name="Другой синтетический клиент")

    async def reuse_selected_contact(messages, execute_tool):
        context = current_context(messages, contact=CONTACT)
        arguments = {"phone": context["phone"], "summary": "Чистка в субботу после 15:00",
                     "contact_id": context["contact_id"]}
        if forged_permission:
            arguments["contact_selection_confirmed"] = True
        result = await execute_tool("prepare_callback", arguments)
        assert result["status"] == "error"
        assert result["code"] == "ambiguous_contact"
        return CALLBACK_REPLIES["ask_contact"]

    app, crm = make_app(tmp_path, ScriptedModel(reuse_selected_contact), crm=SyntheticCRM([CONTACT, second_contact]))
    with TestClient(app) as client:
        sid = new_session(client)
        old = prepare_form(client, sid, contact_id=CONTACT.id)
        response = send(client, sid, "В субботу вместо пятницы")
        if forged_permission:
            assert response.status_code == 503
        else:
            assert response.status_code == 200, response.text
            assert response.json()["callback_error"]["code"] == "ambiguous_contact"
            assert response.json()["callback"]["id"] == old["id"]
            assert response.json()["callback"]["status"] == "canceled"
        assert crm.writes == []


def test_ambiguous_search_context_survives_old_canceled_id_from_browser(tmp_path):
    second_contact = ContactSummary(id="synthetic_other", name="Другой синтетический клиент")
    updated_summary = "Чистка в субботу после 15:00"

    async def prepare_changed_wishes(messages, execute_tool):
        current_context(messages, contact=CONTACT)
        result = await execute_tool("prepare_callback", {
            "phone": PHONE, "summary": updated_summary, "contact_id": None,
        })
        assert result["code"] == "ambiguous_contact"
        return CALLBACK_REPLIES["ask_contact"]

    async def still_needs_human_selection(messages, execute_tool):
        decoder = json.JSONDecoder()
        found = []
        for message in messages:
            if message["role"] != "system":
                continue
            for position, char in enumerate(message["content"]):
                if char != "{":
                    continue
                try:
                    value, _ = decoder.raw_decode(message["content"][position:])
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get("code") == "ambiguous_contact":
                    found.append(value)
        assert len(found) == 1
        assert found[0]["phone"] == PHONE
        assert found[0]["summary"] == updated_summary
        assert {contact["id"] for contact in found[0]["contacts"]} == {CONTACT.id, second_contact.id}
        return CALLBACK_REPLIES["ask_contact"]

    app, crm = make_app(tmp_path, ScriptedModel(prepare_changed_wishes, still_needs_human_selection),
                        crm=SyntheticCRM([CONTACT]))
    with TestClient(app) as client:
        sid = new_session(client)
        old = prepare_form(client, sid)
        crm.contacts.append(second_contact)
        response = send(client, sid, "В субботу вместо пятницы")
        assert response.status_code == 200, response.text
        assert response.json()["callback_error"]["code"] == "ambiguous_contact"
        assert response.json()["callback"]["id"] == old["id"]
        response = send(client, sid, "Открою форму для выбора клиента", callback_id=old["id"])
        assert response.status_code == 200, response.text
        assert crm.writes == []
