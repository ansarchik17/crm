"""Synthetic HTTP acceptance tests; no external AI or CRM requests."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from voicecrm.config import Settings
from voicecrm.crm.callback_store import CallbackStore
from voicecrm.crm.callbacks import CallbackService
from voicecrm.crm.client import CallbackTaskRecord, ContactSummary, CRMError, RecordReceipt
from voicecrm.main import create_app
from voicecrm.dialogue.callback_tools import CALLBACK_REPLIES
from voicecrm.dialogue.sessions import SessionStore
from voicecrm.errors import ProviderError


DRAFT = {"phone": "+15550000123", "summary": "Обратный звонок завтра после 15:00", "contact_id": None}


class FakeCRM:
    configured = True

    def __init__(self, contacts=None):
        self.contacts = [] if contacts is None else contacts
        self.reads = []
        self.writes = []
        self.tasks = []
        self.write_error = None
        self.save_before_error = False

    async def get_user(self, user_id):
        self.reads.append(("user", user_id))
        return RecordReceipt(id=user_id)

    async def search_contacts_by_phone(self, phone):
        self.reads.append(("contacts", phone))
        return self.contacts

    async def find_callback_tasks(self, name):
        self.reads.append(("tasks", name))
        return [task for task in self.tasks if task.name == name]

    async def create_task(self, task):
        self.writes.append(task)
        task_id = f"task{len(self.writes)}"
        if not self.write_error or self.save_before_error:
            self.tasks.append(CallbackTaskRecord(id=task_id, **task.model_dump()))
        if self.write_error:
            raise CRMError(self.write_error)
        return RecordReceipt(id=task_id)


class FakeModel:
    """A scripted model can request drafts, never a confirmation tool."""

    def __init__(self, actions=None):
        self.actions = list(actions or [])
        self.calls = []
        self.results = []
        self.plain_calls = []

    async def reply(self, messages):
        self.plain_calls.append(messages)
        return "Обычный текстовый ответ."

    async def reply_with_tools(self, messages, execute_tool):
        self.calls.append(messages)
        arguments = self.actions.pop(0) if self.actions else None
        if arguments is None:
            return "Уточните пожелания для обращения."
        self.results.append(await execute_tool("prepare_callback", arguments))
        # Deliberate false statement: a successful draft must override this text.
        return "Модель заявляет: задача уже создана в CRM!"


def app_with_callbacks(tmp_path, *, crm=None, model=None):
    crm = crm or FakeCRM()
    model = model or FakeModel()
    service = CallbackService(crm, CallbackStore(tmp_path / "callbacks.sqlite3"), "employee1")
    config = Settings(_env_file=None, llm_mode="demo", stt_enabled=False, callback_assignee_id="")
    return create_app(config, model=model, crm=crm, callbacks=service), crm, model


def prepare(client, draft=None):
    response = client.post("/crm/callbacks", json=DRAFT if draft is None else draft)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()["callback"]


def session(client):
    response = client.post("/sessions")
    assert response.status_code == 201
    return response.json()["session_id"]


def message(client, sid, text, **extra):
    return client.post(f"/sessions/{sid}/messages", json={"text": text, **extra})


@pytest.mark.parametrize("turns", [19, 20])
@pytest.mark.parametrize("command,status,writes", [
    ("Подтверждаю заявку!", "succeeded", 1),
    ("Отменяю заявку", "canceled", 0),
    ("Отменить заявку", "canceled", 0),
    ("Проверить результат", "draft", 0),
])
def test_callback_controls_remain_available_at_turn_limit(tmp_path, monkeypatch, turns, command, status, writes):
    from uuid import UUID

    store = SessionStore()
    monkeypatch.setattr("voicecrm.main.SessionStore", lambda **kwargs: store)
    app, crm, model = app_with_callbacks(tmp_path)
    with TestClient(app) as client:
        sid = session(client)
        for _ in range(turns):
            assert message(client, sid, "Тестовое обращение").status_code == 200
        draft = prepare(client, {**DRAFT, "session_id": sid})
        if turns == 20:
            assert message(client, sid, "Лучше вечером").status_code == 409
            assert client.get(f"/crm/callbacks/{draft['id']}").json()["callback"]["status"] == "draft"
        for _ in range(3):
            response = message(client, sid, command, callback_id=draft["id"])
            assert response.status_code == 200
            assert response.json()["callback"]["status"] == status
        assert len(crm.writes) == writes
        assert len(model.calls) == turns
        state = store.get(UUID(sid))
        assert len(state.messages) == 40
        assert state.callback_history_start <= len(state.messages)
        assert message(client, sid, "Продолжим диалог").status_code == 409


def test_unconfirmed_callback_can_be_reconciled_after_turn_limit_without_reposting(tmp_path):
    crm = FakeCRM()
    crm.write_error = "write_unconfirmed"
    crm.save_before_error = True
    app, _, model = app_with_callbacks(tmp_path, crm=crm)
    with TestClient(app) as client:
        sid = session(client)
        for _ in range(20):
            assert message(client, sid, "Тестовое обращение").status_code == 200
        draft = prepare(client, {**DRAFT, "session_id": sid})
        initial = client.post(f"/crm/callbacks/{draft['id']}/confirm", json={})
        assert initial.json()["callback"]["status"] == "unconfirmed"
        for command in ("Проверить результат", "Подтверждаю заявку"):
            response = message(client, sid, command, callback_id=draft["id"])
            assert response.status_code == 200
            assert response.json()["callback"]["status"] == "succeeded"
            assert response.json()["callback"]["task_id"] == "task1"
        assert len(crm.writes) == 1
        assert len(model.calls) == 20


def test_direct_callback_requires_explicit_confirmation_and_repeats_are_idempotent(tmp_path):
    crm = FakeCRM([ContactSummary(id="contact1", name="Тестовый клиент")])
    app, _, _ = app_with_callbacks(tmp_path, crm=crm)
    with TestClient(app) as client:
        draft = prepare(client)
        assert draft["status"] == "draft"
        assert draft["contact_id"] == "contact1"
        assert draft["phone"] == DRAFT["phone"]
        assert draft["summary"] == DRAFT["summary"]
        assert draft["task_id"] is None
        assert crm.writes == []
        for _ in range(2):
            response = client.post(f"/crm/callbacks/{draft['id']}/confirm", json={})
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["callback"]["status"] == "succeeded"
            assert response.json()["callback"]["task_id"] == "task1"
        assert len(crm.writes) == 1
        task = crm.writes[0]
        assert task.assignedUserId == "employee1"
        assert (task.parentType, task.parentId) == ("Contact", "contact1")
        assert draft["id"] in task.name
        assert DRAFT["phone"] in task.description
        assert DRAFT["summary"] in task.description
        assert "assigned_user_id" not in draft


def test_canceled_draft_cannot_create_a_task(tmp_path):
    app, crm, _ = app_with_callbacks(tmp_path)
    with TestClient(app) as client:
        draft = prepare(client)
        canceled = client.post(f"/crm/callbacks/{draft['id']}/cancel", json={})
        assert canceled.status_code == 200
        assert canceled.json()["callback"]["status"] == "canceled"
        assert client.post(f"/crm/callbacks/{draft['id']}/confirm", json={}).json()["callback"]["status"] == "canceled"
        assert crm.writes == []


def test_callback_is_readable_after_app_restart_with_new_store_instance(tmp_path):
    app, crm, _ = app_with_callbacks(tmp_path)
    with TestClient(app) as client:
        draft = prepare(client)
    restarted, _, _ = app_with_callbacks(tmp_path, crm=crm)
    with TestClient(restarted) as client:
        response = client.get(f"/crm/callbacks/{draft['id']}")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["callback"] == draft
        assert crm.writes == []


@pytest.mark.parametrize("headers", [
    {"host": "outside.example"},
    {"origin": "https://outside.example"},
    {"origin": "http://testserver", "sec-fetch-site": "cross-site"},
])
def test_callback_post_rejects_nonlocal_host_or_origin_before_crm(tmp_path, headers):
    app, crm, _ = app_with_callbacks(tmp_path)
    with TestClient(app) as client:
        response = client.post("/crm/callbacks", json=DRAFT, headers=headers)
        assert response.status_code == 403
        assert crm.reads == []
        assert crm.writes == []


def test_callback_rejects_nonlocal_peer_even_with_local_host(tmp_path):
    app, crm, _ = app_with_callbacks(tmp_path)
    with TestClient(app, client=("203.0.113.7", 45678)) as client:
        response = client.post("/crm/callbacks", json=DRAFT)
        assert response.status_code == 403
        assert crm.reads == []
        assert crm.writes == []


@pytest.mark.parametrize("action", ["confirm", "cancel", "reconcile"])
def test_callback_action_rejects_cross_origin_without_changing_draft(tmp_path, action):
    app, crm, _ = app_with_callbacks(tmp_path)
    with TestClient(app) as client:
        draft = prepare(client)
        response = client.post(f"/crm/callbacks/{draft['id']}/{action}", json={},
                               headers={"origin": "http://outside.example"})
        assert response.status_code == 403
        assert client.get(f"/crm/callbacks/{draft['id']}").json()["callback"]["status"] == "draft"
        assert crm.writes == []


def test_callback_read_rejects_cross_origin_and_dialogue_rejects_remote_peer(tmp_path):
    app, crm, model = app_with_callbacks(tmp_path)
    with TestClient(app) as client:
        draft = prepare(client)
        assert client.get(f"/crm/callbacks/{draft['id']}", headers={"origin": "http://outside.example"}).status_code == 403
        sid = session(client)
    with TestClient(app, client=("203.0.113.7", 45678)) as client:
        assert client.post("/sessions").status_code == 403
        assert message(client, sid, "Перезвоните").status_code == 403
        assert model.calls == []
        assert crm.writes == []


@pytest.mark.parametrize("content_type", ["text/plain", "application/problem+json", "application/x-www-form-urlencoded"])
def test_callback_rejects_non_json_content_type_before_crm(tmp_path, content_type):
    app, crm, _ = app_with_callbacks(tmp_path)
    with TestClient(app) as client:
        response = client.post("/crm/callbacks", json=DRAFT, headers={"content-type": content_type})
        assert response.status_code in (415, 422)
        assert crm.reads == []
        assert crm.writes == []


@pytest.mark.parametrize("field,value", [("assignedUserId", "admin"), ("assignee_id", "admin"), ("confirmed", True)])
def test_public_draft_rejects_model_or_client_assignee_and_confirmation(tmp_path, field, value):
    app, crm, _ = app_with_callbacks(tmp_path)
    with TestClient(app) as client:
        response = client.post("/crm/callbacks", json={**DRAFT, field: value})
        assert response.status_code == 422
        assert crm.reads == []
        assert crm.writes == []


def test_confirmation_rejects_extra_payload_and_unknown_action(tmp_path):
    app, crm, _ = app_with_callbacks(tmp_path)
    with TestClient(app) as client:
        draft = prepare(client)
        path = f"/crm/callbacks/{draft['id']}"
        assert client.post(path + "/confirm", json={"summary": "Подменённые пожелания"}).status_code == 422
        assert client.post(path + "/create_task", json={}).status_code == 404
        assert crm.writes == []


def test_model_prepares_draft_but_only_exact_user_phrase_confirms(tmp_path):
    model = FakeModel([DRAFT])
    app, crm, _ = app_with_callbacks(tmp_path, model=model)
    with TestClient(app) as client:
        sid = session(client)
        response = message(client, sid, "Пожалуйста, перезвоните завтра после 15:00")
        assert response.status_code == 200
        body = response.json()
        draft = body["callback"]
        assert draft["status"] == "draft"
        assert body["callback_error"] is None
        assert DRAFT["phone"] in body["text"]
        assert DRAFT["summary"] in body["text"]
        assert "Подтверждаю заявку" in body["text"]
        assert "уже создана" not in body["text"]
        assert model.results[0]["status"] == "draft"
        assert model.results[0]["draft_id"] == draft["id"]
        assert crm.writes == []
        response = message(client, sid, "Подтверждаю заявку")
        assert response.status_code == 200
        assert response.json()["callback"]["status"] == "succeeded"
        assert len(crm.writes) == 1
        assert len(model.calls) == 1
        assert message(client, sid, "Подтверждаю заявку").json()["callback"]["status"] == "succeeded"
        assert len(crm.writes) == 1


def test_arbitrary_yes_invalidates_old_draft_without_sending_it(tmp_path):
    model = FakeModel([DRAFT, None])
    app, crm, _ = app_with_callbacks(tmp_path, model=model)
    with TestClient(app) as client:
        sid = session(client)
        original = message(client, sid, "Перезвоните").json()["callback"]
        response = message(client, sid, "да")
        assert response.status_code == 200
        assert response.json()["callback"]["status"] == "canceled"
        assert crm.writes == []
        assert client.get(f"/crm/callbacks/{original['id']}").json()["callback"]["status"] == "canceled"
        response = message(client, sid, "Подтверждаю заявку")
        assert response.json()["callback"] is None
        assert crm.writes == []


def test_changed_wishes_cancel_old_preview_and_confirm_only_latest_data(tmp_path):
    changed = {**DRAFT, "summary": "Обратный звонок завтра после 17:00 вместо 15:00"}
    model = FakeModel([DRAFT, changed])
    app, crm, _ = app_with_callbacks(tmp_path, model=model)
    with TestClient(app) as client:
        sid = session(client)
        old = message(client, sid, "Перезвоните после 15:00").json()["callback"]
        response = message(client, sid, "Лучше после 17:00")
        assert response.status_code == 200
        current = response.json()["callback"]
        assert current["id"] != old["id"]
        assert current["summary"] == changed["summary"]
        assert client.get(f"/crm/callbacks/{old['id']}").json()["callback"]["status"] == "canceled"
        assert crm.writes == []
        assert message(client, sid, "Подтверждаю заявку").json()["callback"]["status"] == "succeeded"
        assert len(crm.writes) == 1
        assert changed["summary"] in crm.writes[0].description
        assert old["id"] not in crm.writes[0].name


def test_new_session_can_resume_persisted_operation_using_callback_id(tmp_path):
    app, crm, model = app_with_callbacks(tmp_path)
    with TestClient(app) as client:
        draft = prepare(client)
    restarted, _, _ = app_with_callbacks(tmp_path, crm=crm, model=model)
    with TestClient(restarted) as client:
        sid = session(client)
        response = message(client, sid, "Подтверждаю заявку", callback_id=draft["id"])
        assert response.status_code == 200
        assert response.json()["callback"]["id"] == draft["id"]
        assert response.json()["callback"]["status"] == "succeeded"
        assert len(crm.writes) == 1
        assert model.calls == []


def test_stale_callback_id_cannot_replace_current_session_preview(tmp_path):
    model = FakeModel([DRAFT])
    app, crm, _ = app_with_callbacks(tmp_path, model=model)
    with TestClient(app) as client:
        stale = prepare(client)
        sid = session(client)
        current = message(client, sid, "Перезвоните").json()["callback"]
        response = message(client, sid, "Подтверждаю заявку", callback_id=stale["id"])
        assert response.status_code == 409
        assert client.get(f"/crm/callbacks/{current['id']}").json()["callback"]["status"] == "draft"
        assert crm.writes == []


def test_unresolved_operation_blocks_new_draft_and_keeps_original_identity(tmp_path):
    model = FakeModel([DRAFT, {**DRAFT, "summary": "Другой запрос после неподтверждённой отправки"}])
    crm = FakeCRM()
    crm.write_error = "write_unconfirmed"
    app, _, _ = app_with_callbacks(tmp_path, crm=crm, model=model)
    with TestClient(app) as client:
        sid = session(client)
        original = message(client, sid, "Перезвоните").json()["callback"]
        confirmed = message(client, sid, "Подтверждаю заявку")
        assert confirmed.json()["callback"]["status"] == "unconfirmed"
        reads_before = list(crm.reads)
        response = message(client, sid, "Теперь подготовьте другой запрос")
        assert response.status_code in (200, 409)
        if response.status_code == 200:
            assert response.json()["callback"]["id"] == original["id"]
            assert response.json()["callback"]["status"] == "unconfirmed"
        assert len(model.results) == 1
        assert [read for read in crm.reads if read[0] in ("user", "contacts")] == reads_before
        assert len(crm.writes) == 1
        retried = message(client, sid, "Подтверждаю заявку")
        assert retried.status_code == 200
        assert retried.json()["callback"]["id"] == original["id"]
        assert retried.json()["callback"]["status"] == "unconfirmed"
        assert len(crm.writes) == 1


def test_manual_form_draft_attaches_to_session_and_replaces_previous_preview(tmp_path):
    model = FakeModel([DRAFT])
    app, crm, _ = app_with_callbacks(tmp_path, model=model)
    with TestClient(app) as client:
        sid = session(client)
        previous = message(client, sid, "Перезвоните после 15:00").json()["callback"]
        current = prepare(client, {**DRAFT, "summary": "Перезвонить после 18:00", "session_id": sid})
        assert current["id"] != previous["id"]
        assert client.get(f"/crm/callbacks/{previous['id']}").json()["callback"]["status"] == "canceled"
        confirmed = message(client, sid, "Подтверждаю заявку")
        assert confirmed.status_code == 200
        assert confirmed.json()["callback"]["id"] == current["id"]
        assert confirmed.json()["callback"]["status"] == "succeeded"
        assert len(crm.writes) == 1
        assert "после 18:00" in crm.writes[0].description


def test_ambiguous_contact_error_provides_safe_candidates_and_context_for_next_turn(tmp_path):
    crm = FakeCRM([ContactSummary(id="contact1", name="Клиент Один", emailAddress="synthetic@example.invalid"),
                   ContactSummary(id="contact2", name="Клиент Два")])
    model = FakeModel([DRAFT, {**DRAFT, "contact_id": "contact2"}])
    app, _, _ = app_with_callbacks(tmp_path, crm=crm, model=model)
    with TestClient(app) as client:
        direct = client.post("/crm/callbacks", json=DRAFT)
        assert direct.status_code == 409
        assert direct.json()["detail"]["contacts"] == [
            {"id": "contact1", "name": "Клиент Один"}, {"id": "contact2", "name": "Клиент Два"}]
        sid = session(client)
        body = message(client, sid, "Перезвоните").json()
        assert body["callback"] is None
        assert body["callback_error"]["code"] == "ambiguous_contact"
        assert body["callback_error"]["contacts"] == direct.json()["detail"]["contacts"]
        assert body["callback_error"]["phone"] == DRAFT["phone"]
        assert body["callback_error"]["summary"] == DRAFT["summary"]
        assert "уже создана" not in body["text"]
        assert crm.writes == []
        # A spoken name does not give the model authority to select an ID.
        rejected = message(client, sid, "Я Клиент Два").json()
        assert rejected["callback"] is None
        assert rejected["callback_error"]["code"] == "ambiguous_contact"
        context = model.calls[-1][0]
        assert context["role"] == "system"
        assert "contact1" in context["content"] and "contact2" in context["content"]
        assert "synthetic@example.invalid" not in context["content"]
        assert crm.writes == []
        selected = prepare(client, {**DRAFT, "contact_id": "contact2", "session_id": sid})
        assert selected["status"] == "draft"
        assert selected["contact_id"] == "contact2"
        assert selected["contact_name"] == "Клиент Два"
        confirmed = message(client, sid, "Подтверждаю заявку").json()
        assert confirmed["callback"]["status"] == "succeeded"
        assert len(crm.writes) == 1
        assert crm.writes[0].parentId == "contact2"


def test_unconfirmed_write_reconciles_after_restart_without_reposting(tmp_path):
    crm = FakeCRM()
    crm.write_error = "write_unconfirmed"
    crm.save_before_error = True
    app, _, _ = app_with_callbacks(tmp_path, crm=crm)
    with TestClient(app) as client:
        draft = prepare(client)
        response = client.post(f"/crm/callbacks/{draft['id']}/confirm", json={})
        assert response.status_code == 200
        assert response.json()["callback"]["status"] == "unconfirmed"
        assert "не подтвердила" in response.json()["text"]
        assert len(crm.writes) == 1
    restarted, _, _ = app_with_callbacks(tmp_path, crm=crm)
    with TestClient(restarted) as client:
        response = client.post(f"/crm/callbacks/{draft['id']}/reconcile", json={})
        assert response.status_code == 200
        assert response.json()["callback"]["status"] == "succeeded"
        assert response.json()["callback"]["task_id"] == "task1"
        assert len(crm.writes) == 1


def test_callback_disabled_preserves_normal_model_reply(tmp_path):
    model = FakeModel([DRAFT])
    config = Settings(_env_file=None, llm_mode="demo", stt_enabled=False, callback_assignee_id="")
    with TestClient(create_app(config, model=model, crm=FakeCRM())) as client:
        sid = session(client)
        response = message(client, sid, "Перезвоните")
        assert response.status_code == 200
        assert response.json()["text"] == "Обычный текстовый ответ."
        assert response.json()["callback"] is None
        assert model.calls == []
        assert len(model.plain_calls) == 1
        assert client.post("/crm/callbacks", json=DRAFT).status_code == 503


def test_missing_callback_is_reported_without_model_or_write(tmp_path):
    app, crm, model = app_with_callbacks(tmp_path)
    with TestClient(app) as client:
        missing = str(uuid4())
        assert client.get(f"/crm/callbacks/{missing}").status_code == 404
        sid = session(client)
        response = message(client, sid, "Подтверждаю заявку", callback_id=missing)
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "not_found"
        assert model.calls == []
        assert crm.writes == []
        # The invalid operation is never attached to this otherwise valid session.
        recovered = message(client, sid, "Здравствуйте")
        assert recovered.status_code == 200
        assert recovered.json()["callback"] is None
        assert len(model.calls) == 1


def test_untrusted_model_text_cannot_report_success_without_crm_receipt(tmp_path):
    class UntrustedModel:
        async def reply_with_tools(self, messages, execute_tool):
            return "Задача создана в CRM! Вы записаны на завтра."

    app, crm, _ = app_with_callbacks(tmp_path, model=UntrustedModel())
    with TestClient(app) as client:
        sid = session(client)
        response = message(client, sid, "Пожалуйста, перезвоните")
        assert response.status_code == 200
        assert response.json()["text"] == CALLBACK_REPLIES["callback_help"]
        assert response.json()["callback"] is None
        assert crm.writes == []


def test_model_cannot_choose_contact_in_a_second_tool_call_without_user_action(tmp_path):
    class AutoChoosingModel:
        async def reply_with_tools(self, messages, execute_tool):
            first = await execute_tool("prepare_callback", DRAFT)
            assert first["code"] == "ambiguous_contact"
            second = await execute_tool("prepare_callback", {**DRAFT, "contact_id": first["contacts"][0]["id"]})
            assert second["code"] == "ambiguous_contact"
            return "Клиент выбран и заявка создана."

    crm = FakeCRM([ContactSummary(id="contact1", name="Синтетический клиент 1"),
                   ContactSummary(id="contact2", name="Синтетический клиент 2")])
    app, _, _ = app_with_callbacks(tmp_path, crm=crm, model=AutoChoosingModel())
    with TestClient(app) as client:
        response = message(client, session(client), "Перезвоните")
        assert response.status_code == 200
        assert response.json()["callback"] is None
        assert response.json()["callback_error"]["code"] == "ambiguous_contact"
        assert response.json()["text"] == CALLBACK_REPLIES["ask_contact"]
        assert crm.writes == []


def test_tool_boundary_rejects_a_model_supplied_contact_confirmation_flag(tmp_path):
    class FlagForgingModel:
        async def reply_with_tools(self, messages, execute_tool):
            await execute_tool("prepare_callback", {**DRAFT, "contact_id": "contact1",
                                                     "contact_selection_confirmed": True})
            return "Заявка создана"

    crm = FakeCRM([ContactSummary(id="contact1", name="Синтетический клиент 1"),
                   ContactSummary(id="contact2", name="Синтетический клиент 2")])
    app, _, _ = app_with_callbacks(tmp_path, crm=crm, model=FlagForgingModel())
    with TestClient(app) as client:
        response = message(client, session(client), "Перезвоните")
        assert response.status_code == 503
        assert "Заявка создана" not in response.text
        assert crm.reads == []
        assert crm.writes == []


def test_model_without_callback_contract_does_not_narrate_crm_actions(tmp_path):
    class NoToolsModel:
        async def reply(self, messages):
            raise AssertionError("Plain model must not run for the enabled callback workflow")

    app, crm, _ = app_with_callbacks(tmp_path, model=NoToolsModel())
    with TestClient(app) as client:
        response = message(client, session(client), "Подготовьте заявку")
        assert response.status_code == 200
        assert response.json()["text"] == CALLBACK_REPLIES["callback_help"]
        assert response.json()["callback"] is None
        assert crm.writes == []


def test_slow_form_preparation_refreshes_session_after_crm_reads(tmp_path, monkeypatch):
    now = [0.0]
    store = SessionStore(idle_timeout_seconds=10, clock=lambda: now[0])
    monkeypatch.setattr("voicecrm.main.SessionStore", lambda **kwargs: store)

    class SlowCRM(FakeCRM):
        async def get_user(self, user_id):
            now[0] += 20
            return await super().get_user(user_id)

    app, crm, _ = app_with_callbacks(tmp_path, crm=SlowCRM())
    with TestClient(app) as client:
        sid = session(client)
        draft = prepare(client, {**DRAFT, "session_id": sid})
        response = message(client, sid, "Подтверждаю заявку")
        assert response.status_code == 200
        assert response.json()["callback"]["id"] == draft["id"]
        assert response.json()["callback"]["status"] == "succeeded"
        assert len(crm.writes) == 1


def test_slow_failed_model_request_preserves_session_for_retry(tmp_path, monkeypatch):
    now = [0.0]
    store = SessionStore(idle_timeout_seconds=10, clock=lambda: now[0])
    monkeypatch.setattr("voicecrm.main.SessionStore", lambda **kwargs: store)

    class SlowModel:
        calls = 0

        async def reply_with_tools(self, messages, execute_tool):
            self.calls += 1
            if self.calls == 1:
                now[0] += 20
                raise ProviderError("Synthetic timeout")
            assert len(messages) == 1
            return CALLBACK_REPLIES["ask_phone"]

    app, crm, _ = app_with_callbacks(tmp_path, model=SlowModel())
    with TestClient(app) as client:
        sid = session(client)
        assert message(client, sid, "Перезвоните").status_code == 503
        retry = message(client, sid, "Перезвоните ещё раз")
        assert retry.status_code == 200
        assert retry.json()["text"] == CALLBACK_REPLIES["ask_phone"]
        assert crm.writes == []
