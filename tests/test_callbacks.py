import asyncio
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from voicecrm.crm.callback_router import build_callback_router, callback_exception, callback_public, callback_text
from voicecrm.crm.callback_store import CallbackOperation, CallbackStore, CallbackStoreError
from voicecrm.crm.callbacks import CallbackError, CallbackService, callback_task_payload
from voicecrm.crm.client import CRMError, ContactSummary, RecordReceipt
from voicecrm.dialogue.sessions import SessionStore


PHONE = "+15555550123"
SUMMARY = "Synthetic callback request"
ASSIGNEE = "employee1"


def contact(identifier="contact1", name="Synthetic Contact"):
    return ContactSummary(id=identifier, name=name, phoneNumber=PHONE, emailAddress="synthetic@example.test")


def remote_record(operation, **overrides):
    payload = callback_task_payload(operation).model_dump()
    payload.update({"id": "task1", **overrides})
    return SimpleNamespace(**payload)


class FakeClient:
    def __init__(self, contacts=()):
        self.contacts = list(contacts)
        self.posts = []
        self.finds = []
        self.users = []
        self.records = []
        self.user_id = ASSIGNEE
        self.user_error = None
        self.write_error = None
        self.save_before_error = False
        self.entered = None
        self.release = None
        self.find_entered = None
        self.find_release = None
        self.find_error = None

    async def get_user(self, identifier):
        self.users.append(identifier)
        if self.user_error:
            raise self.user_error
        return RecordReceipt(id=self.user_id)

    async def search_contacts_by_phone(self, phone):
        assert phone == PHONE
        return self.contacts

    async def create_task(self, task):
        self.posts.append(task)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.write_error is None or self.save_before_error:
            self.records.append(SimpleNamespace(id="task1", **task.model_dump()))
        if self.write_error:
            raise self.write_error
        return RecordReceipt(id="task1")

    async def find_callback_tasks(self, name):
        self.finds.append(name)
        if self.find_error:
            raise self.find_error
        result = list(self.records)
        if self.find_entered is not None:
            self.find_entered.set()
        if self.find_release is not None:
            await self.find_release.wait()
        return result


def service(tmp_path, client=None, assignee=ASSIGNEE):
    return CallbackService(client or FakeClient(), CallbackStore(tmp_path / "callbacks.sqlite3"), assignee)


def test_prepare_is_immutable_and_happy_confirmation_is_persisted(tmp_path):
    async def run():
        client = FakeClient([contact()])
        callbacks = service(tmp_path, client)
        draft = await callbacks.prepare("+1 (555) 555-0123", "  " + SUMMARY + "  ")
        assert draft.status == "draft"
        assert draft.phone == PHONE
        assert draft.summary == SUMMARY
        assert draft.contact_id == "contact1"
        assert draft.contact_name == "Synthetic Contact"
        assert draft.assigned_user_id == ASSIGNEE
        assert client.users == [ASSIGNEE]
        assert client.posts == []
        assert "555" not in repr(draft) and SUMMARY not in repr(draft)
        assert "Synthetic Contact" not in repr(draft)
        with pytest.raises(ValidationError):
            draft.summary = "Changed"
        result = await callbacks.confirm(draft.id)
        assert result.status == "succeeded" and result.task_id == "task1" and result.error is None
        sent = client.posts[0]
        assert sent.name == f"Обратный звонок [VC:{draft.id}]"
        assert sent.description == f"VoiceCRM operation: [VC:{draft.id}]\nТелефон: {PHONE}\nПожелания: {SUMMARY}"
        assert sent.parentType == "Contact" and sent.parentId == "contact1"
        assert sent.assignedUserId == ASSIGNEE
        assert (await callbacks.confirm(draft.id)) == result
        assert client.finds == [] and len(client.posts) == 1
        assert await service(tmp_path, client).get(draft.id) == result

    asyncio.run(run())


def test_unknown_contact_can_create_an_unlinked_task(tmp_path):
    async def run():
        client = FakeClient()
        callbacks = service(tmp_path, client)
        draft = await callbacks.prepare(PHONE, SUMMARY)
        assert draft.contact_id is None and draft.contact_name is None
        assert (await callbacks.confirm(draft.id)).status == "succeeded"
        assert client.posts[0].parentId is None and client.posts[0].parentType is None

    asyncio.run(run())


def test_ambiguous_contacts_require_explicit_matching_selection(tmp_path):
    async def run():
        client = FakeClient([contact("contact1"), contact("contact2", "Synthetic Two")])
        callbacks = service(tmp_path, client)
        with pytest.raises(CallbackError, match="^ambiguous_contact$") as error:
            await callbacks.prepare(PHONE, SUMMARY)
        assert [choice.model_dump() for choice in error.value.contacts] == [
            {"id": "contact1", "name": "Synthetic Contact"}, {"id": "contact2", "name": "Synthetic Two"},
        ]
        assert PHONE not in str(error.value)
        # Knowing a candidate ID is not evidence that the caller selected it.
        with pytest.raises(CallbackError, match="^ambiguous_contact$"):
            await callbacks.prepare(PHONE, SUMMARY, contact_id="contact2")
        with pytest.raises(CallbackError, match="^invalid_contact$"):
            await callbacks.prepare(PHONE, SUMMARY, contact_id="unrelated", contact_selection_confirmed=True)
        draft = await callbacks.prepare(PHONE, SUMMARY, contact_id="contact2", contact_selection_confirmed=True)
        assert draft.contact_id == "contact2" and draft.contact_name == "Synthetic Two"
        assert (await callbacks.confirm(draft.id)).status == "succeeded"
        assert client.posts[0].parentId == "contact2"

    asyncio.run(run())


@pytest.mark.parametrize("matches", [[], [contact()]])
def test_explicit_contact_must_belong_to_phone_results(tmp_path, matches):
    async def run():
        callbacks = service(tmp_path, FakeClient(matches))
        with pytest.raises(CallbackError, match="^invalid_contact$"):
            await callbacks.prepare(PHONE, SUMMARY, contact_id="unrelated")

    asyncio.run(run())


@pytest.mark.parametrize("selection_confirmed", [False, True])
def test_full_contact_page_is_ambiguous_even_with_explicit_selection(tmp_path, selection_confirmed):
    async def run():
        client = FakeClient([contact(f"contact{number}") for number in range(20)])
        callbacks = service(tmp_path, client)
        with pytest.raises(CallbackError, match="^ambiguous_contact$"):
            await callbacks.prepare(PHONE, SUMMARY, contact_id="contact1",
                                    contact_selection_confirmed=selection_confirmed)
        assert client.posts == []

    asyncio.run(run())


@pytest.mark.parametrize("selection_confirmed", [False, None, 1, "true"])
def test_ambiguous_contact_selection_requires_the_exact_server_flag(tmp_path, selection_confirmed):
    async def run():
        client = FakeClient([contact(), contact("contact2", "Synthetic Two")])
        callbacks = service(tmp_path, client)
        with pytest.raises(CallbackError, match="^ambiguous_contact$"):
            await callbacks.prepare(PHONE, SUMMARY, contact_id="contact2",
                                    contact_selection_confirmed=selection_confirmed)
        with sqlite3.connect(callbacks.store.path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM callback_operations").fetchone()[0] == 0
        assert client.posts == []

    asyncio.run(run())


def test_human_selection_flag_without_a_contact_id_does_not_resolve_ambiguity(tmp_path):
    async def run():
        callbacks = service(tmp_path, FakeClient([contact(), contact("contact2")]))
        with pytest.raises(CallbackError, match="^ambiguous_contact$"):
            await callbacks.prepare(PHONE, SUMMARY, contact_selection_confirmed=True)

    asyncio.run(run())


@pytest.mark.parametrize("matches,expected_name,expected_text", [
    ([contact()], "Synthetic Contact", "Контакт: Synthetic Contact."),
    ([contact(name=None)], None, "Контакт выбран; имя недоступно."),
    ([], None, "Без привязки к контакту."),
])
def test_callback_preview_exposes_saved_contact_or_explicitly_absent_link(tmp_path, matches,
                                                                        expected_name, expected_text):
    callbacks = service(tmp_path, FakeClient(matches))
    draft = asyncio.run(callbacks.prepare(PHONE, SUMMARY))
    restored = asyncio.run(service(tmp_path).get(draft.id))
    assert callback_public(restored)["contact_name"] == expected_name
    assert expected_text in callback_text(restored)
    assert restored == draft


def test_missing_callback_error_has_a_machine_readable_code():
    error = callback_exception(CallbackError("not_found"))
    assert error.status_code == 404
    assert error.detail == {"code": "not_found", "message": "Обращение не найдено.", "contacts": []}


def test_direct_local_route_confirms_only_a_supplied_contact_selection(tmp_path):
    crm = FakeClient([contact(), contact("contact2", "Synthetic Two")])
    app = FastAPI()
    app.state.callbacks = service(tmp_path, crm)
    app.include_router(build_callback_router(SessionStore()))
    with TestClient(app) as client:
        unresolved = client.post("/crm/callbacks", json={"phone": PHONE, "summary": SUMMARY})
        assert unresolved.status_code == 409
        assert unresolved.json()["detail"]["code"] == "ambiguous_contact"
        selected = client.post("/crm/callbacks", json={
            "phone": PHONE, "summary": SUMMARY, "contact_id": "contact2",
        })
        assert selected.status_code == 201
        assert selected.json()["callback"]["contact_name"] == "Synthetic Two"
        assert "Контакт: Synthetic Two." in selected.json()["text"]
        assert crm.posts == []


@pytest.mark.parametrize("concurrent_status", ["dispatching", "unconfirmed", "succeeded", "failed"])
def test_replacing_draft_stops_if_another_process_dispatched_it(tmp_path, concurrent_status):
    callbacks = service(tmp_path)
    previous = asyncio.run(callbacks.prepare(PHONE, SUMMARY))

    async def cancel_after_another_process_claimed(operation_id):
        assert operation_id == previous.id
        callbacks.store.transition(operation_id, expected={"draft"}, status="dispatching")
        if concurrent_status != "dispatching":
            callbacks.store.transition(operation_id, expected={"dispatching"}, status=concurrent_status,
                                       task_id="task1" if concurrent_status == "succeeded" else None)
        return await callbacks.get(operation_id)

    callbacks.cancel = cancel_after_another_process_claimed
    app = FastAPI()
    app.state.callbacks = callbacks
    app.include_router(build_callback_router(SessionStore()))
    with TestClient(app) as client:
        result = client.post("/crm/callbacks", json={
            "phone": PHONE, "summary": "Changed request", "previous_id": previous.id,
        })
        assert result.status_code == 409
    with sqlite3.connect(callbacks.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM callback_operations").fetchone()[0] == 1


@pytest.mark.parametrize("phone,summary,error", [
    ("15555550123", SUMMARY, "invalid_phone"),
    (PHONE, " ", "invalid_summary"), (PHONE, "x" * 2001, "invalid_summary"),
    (PHONE, "Synthetic\x00request", "invalid_summary"), (PHONE, None, "invalid_summary"),
])
def test_invalid_request_never_reads_crm(tmp_path, phone, summary, error):
    async def run():
        client = FakeClient()
        with pytest.raises(CallbackError, match="^" + error + "$"):
            await service(tmp_path, client).prepare(phone, summary)
        assert client.users == [] and client.posts == []

    asyncio.run(run())


@pytest.mark.parametrize("assignee,error", [("", "not_configured"), (" ", "not_configured"),
                                          ("bad/id", "invalid_assignee")])
def test_unconfigured_assignee_never_creates_a_draft(tmp_path, assignee, error):
    async def run():
        client = FakeClient()
        with pytest.raises(CallbackError, match="^" + error + "$"):
            await service(tmp_path, client, assignee).prepare(PHONE, SUMMARY)
        assert client.users == [] and client.posts == []

    asyncio.run(run())


def test_assignee_must_be_confirmed_by_crm_and_errors_remain_sanitized(tmp_path):
    async def run():
        client = FakeClient()
        callbacks = service(tmp_path, client)
        client.user_error = CRMError("forbidden")
        with pytest.raises(CRMError, match="^forbidden$"):
            await callbacks.prepare(PHONE, SUMMARY)
        client.user_error = None
        client.user_id = "anotherEmployee"
        with pytest.raises(CallbackError, match="^invalid_assignee$"):
            await callbacks.prepare(PHONE, SUMMARY)
        assert client.posts == []

    asyncio.run(run())


def test_concurrent_confirmation_uses_one_committed_claim_across_store_instances(tmp_path):
    async def run():
        client = FakeClient()
        client.entered, client.release = asyncio.Event(), asyncio.Event()
        callbacks = service(tmp_path, client)
        draft = await callbacks.prepare(PHONE, SUMMARY)
        writing = asyncio.create_task(callbacks.confirm(draft.id))
        await client.entered.wait()
        another = service(tmp_path, client)
        assert (await another.get(draft.id)).status == "dispatching"
        assert (await another.confirm(draft.id)).status == "unconfirmed"
        assert len(client.posts) == 1
        client.release.set()
        assert (await writing).status == "succeeded"
        assert (await another.confirm(draft.id)).status == "succeeded"
        assert len(client.posts) == 1

    asyncio.run(run())


def test_crash_after_claim_before_post_never_retries_after_restart(tmp_path):
    async def run():
        client = FakeClient()
        callbacks = service(tmp_path, client)
        draft = await callbacks.prepare(PHONE, SUMMARY)
        callbacks.store.transition(draft.id, expected={"draft"}, status="dispatching")
        restarted = service(tmp_path, client)
        for _ in range(3):
            result = await restarted.confirm(draft.id)
            assert result.status == "unconfirmed" and result.task_id is None
        assert client.posts == [] and len(client.finds) == 3
        client.records = [remote_record(draft)]
        assert (await restarted.reconcile(draft.id)).status == "succeeded"
        assert client.posts == []

    asyncio.run(run())


def test_timeout_after_remote_save_reconciles_by_exact_persisted_payload(tmp_path):
    async def run():
        client = FakeClient([contact()])
        client.write_error, client.save_before_error = CRMError("write_unconfirmed"), True
        callbacks = service(tmp_path, client)
        draft = await callbacks.prepare(PHONE, SUMMARY)
        assert (await callbacks.confirm(draft.id)).status == "unconfirmed"
        result = await service(tmp_path, client, assignee="changedEmployee").confirm(draft.id)
        assert result.status == "succeeded" and result.task_id == "task1"
        assert len(client.posts) == 1 and len(client.finds) == 1

    asyncio.run(run())


def test_zero_reconciliation_matches_never_rearms_a_write(tmp_path):
    async def run():
        client = FakeClient()
        client.write_error = CRMError("write_unconfirmed")
        callbacks = service(tmp_path, client)
        draft = await callbacks.prepare(PHONE, SUMMARY)
        for _ in range(3):
            result = await callbacks.confirm(draft.id)
            assert result.status == "unconfirmed" and result.error == "write_unconfirmed"
        assert len(client.posts) == 1 and len(client.finds) == 2
        with pytest.raises(CallbackStoreError, match="^storage_unavailable$"):
            callbacks.store.transition(draft.id, expected={"unconfirmed"}, status="draft")

    asyncio.run(run())


@pytest.mark.parametrize("read_error,code", [(CRMError("forbidden"), "forbidden"),
                                            (CRMError("invalid_response"), "invalid_response"),
                                            (RuntimeError("Synthetic private detail"), "unavailable")])
def test_reconciliation_read_failure_preserves_uncertainty_without_post(tmp_path, read_error, code):
    async def run():
        client = FakeClient()
        client.find_error = read_error
        callbacks = service(tmp_path, client)
        draft = await callbacks.prepare(PHONE, SUMMARY)
        callbacks.store.transition(draft.id, expected={"draft"}, status="dispatching")
        result = await callbacks.confirm(draft.id)
        assert result.status == "unconfirmed" and result.error == code
        assert client.posts == []

    asyncio.run(run())


@pytest.mark.parametrize("changed", [
    {"name": "Unrelated task"}, {"description": "No operation marker"},
    {"description": "Wrong [VC:00000000-0000-0000-0000-000000000000] marker"},
    {"assignedUserId": "anotherEmployee"}, {"parentType": "Lead"}, {"parentId": "anotherContact"},
])
def test_reconciliation_requires_marker_and_every_immutable_field(tmp_path, changed):
    async def run():
        client = FakeClient([contact()])
        callbacks = service(tmp_path, client)
        draft = await callbacks.prepare(PHONE, SUMMARY)
        callbacks.store.transition(draft.id, expected={"draft"}, status="dispatching")
        client.records = [remote_record(draft, **changed)]
        result = await callbacks.confirm(draft.id)
        assert result.status == "unconfirmed" and result.error == "reconciliation_conflict"
        assert result.task_id is None and client.posts == []

    asyncio.run(run())


def test_multiple_remote_matches_are_not_treated_as_success(tmp_path):
    async def run():
        client = FakeClient()
        callbacks = service(tmp_path, client)
        draft = await callbacks.prepare(PHONE, SUMMARY)
        callbacks.store.transition(draft.id, expected={"draft"}, status="dispatching")
        client.records = [remote_record(draft), remote_record(draft, id="task2")]
        result = await callbacks.reconcile(draft.id)
        assert result.status == "unconfirmed" and result.error == "reconciliation_conflict"
        assert client.posts == []

    asyncio.run(run())


def test_cancel_only_cancels_drafts_and_canceled_confirmation_does_not_write(tmp_path):
    async def run():
        client = FakeClient()
        callbacks = service(tmp_path, client)
        draft = await callbacks.prepare(PHONE, SUMMARY)
        assert (await callbacks.cancel(draft.id)).status == "canceled"
        assert (await callbacks.confirm(draft.id)).status == "canceled"
        assert client.posts == [] and client.finds == []
        next_draft = await callbacks.prepare(PHONE, SUMMARY)
        client.entered, client.release = asyncio.Event(), asyncio.Event()
        writing = asyncio.create_task(callbacks.confirm(next_draft.id))
        await client.entered.wait()
        assert (await callbacks.cancel(next_draft.id)).status == "dispatching"
        client.release.set()
        assert (await writing).status == "succeeded"
        assert (await callbacks.cancel(next_draft.id)).status == "succeeded"

    asyncio.run(run())


def test_cancellation_during_write_persists_uncertainty_and_never_retries(tmp_path):
    async def run():
        client = FakeClient()
        callbacks = service(tmp_path, client)
        draft = await callbacks.prepare(PHONE, SUMMARY)
        client.entered, client.release = asyncio.Event(), asyncio.Event()
        writing = asyncio.create_task(callbacks.confirm(draft.id))
        await client.entered.wait()
        writing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writing
        assert (await service(tmp_path, client).get(draft.id)).status == "unconfirmed"
        assert (await callbacks.confirm(draft.id)).status == "unconfirmed"
        assert len(client.posts) == 1

    asyncio.run(run())


def test_definite_write_failure_is_terminal(tmp_path):
    async def run():
        client = FakeClient()
        client.write_error = CRMError("forbidden")
        callbacks = service(tmp_path, client)
        draft = await callbacks.prepare(PHONE, SUMMARY)
        result = await callbacks.confirm(draft.id)
        assert result.status == "failed" and result.error == "forbidden"
        assert await callbacks.confirm(draft.id) == result
        assert len(client.posts) == 1 and client.finds == []

    asyncio.run(run())


def test_unexpected_write_error_is_sanitized_and_never_retried(tmp_path):
    async def run():
        client = FakeClient()
        client.write_error = RuntimeError("Synthetic private detail")
        callbacks = service(tmp_path, client)
        draft = await callbacks.prepare(PHONE, SUMMARY)
        result = await callbacks.confirm(draft.id)
        assert result.status == "unconfirmed" and result.error == "write_unconfirmed"
        assert "private" not in result.model_dump_json()
        await callbacks.confirm(draft.id)
        assert len(client.posts) == 1

    asyncio.run(run())


def test_late_zero_match_reconciliation_cannot_regress_success(tmp_path):
    async def run():
        client = FakeClient()
        client.entered, client.release = asyncio.Event(), asyncio.Event()
        client.find_entered, client.find_release = asyncio.Event(), asyncio.Event()
        callbacks = service(tmp_path, client)
        draft = await callbacks.prepare(PHONE, SUMMARY)
        writing = asyncio.create_task(callbacks.confirm(draft.id))
        await client.entered.wait()
        reading = asyncio.create_task(callbacks.reconcile(draft.id))
        await client.find_entered.wait()
        client.release.set()
        assert (await writing).status == "succeeded"
        client.find_release.set()
        assert (await reading).status == "succeeded"
        assert (await callbacks.get(draft.id)).task_id == "task1"

    asyncio.run(run())


def test_changed_assignee_blocks_old_draft_but_not_read_only_reconciliation(tmp_path):
    async def run():
        client = FakeClient()
        original = service(tmp_path, client)
        draft = await original.prepare(PHONE, SUMMARY)
        with pytest.raises(CallbackError, match="^invalid_assignee$"):
            await service(tmp_path, client, "changedEmployee").confirm(draft.id)
        with pytest.raises(CallbackError, match="^not_configured$"):
            await service(tmp_path, client, "").confirm(draft.id)
        assert (await original.get(draft.id)).status == "draft" and client.posts == []
        original.store.transition(draft.id, expected={"draft"}, status="dispatching")
        client.records = [remote_record(draft)]
        assert (await service(tmp_path, client, "").reconcile(draft.id)).status == "succeeded"

    asyncio.run(run())


@pytest.mark.parametrize("initial,changed", [
    ([], [contact()]), ([contact()], []), ([contact()], [contact("anotherContact")]),
])
def test_contact_change_before_confirmation_requires_new_draft(tmp_path, initial, changed):
    async def run():
        client = FakeClient(initial)
        callbacks = service(tmp_path, client)
        draft = await callbacks.prepare(PHONE, SUMMARY)
        client.contacts = changed
        with pytest.raises(CallbackError, match="^contact_changed$"):
            await callbacks.confirm(draft.id)
        assert (await callbacks.get(draft.id)).status == "draft"
        assert client.posts == []

    asyncio.run(run())


def test_contact_search_becoming_truncated_before_confirmation_blocks_write(tmp_path):
    async def run():
        client = FakeClient([contact()])
        callbacks = service(tmp_path, client)
        draft = await callbacks.prepare(PHONE, SUMMARY)
        client.contacts = [contact(f"contact{number}") for number in range(20)]
        with pytest.raises(CallbackError, match="^ambiguous_contact$"):
            await callbacks.confirm(draft.id)
        assert (await callbacks.get(draft.id)).status == "draft"
        assert client.posts == []

    asyncio.run(run())


def test_assignee_no_longer_readable_before_confirmation_leaves_draft_untouched(tmp_path):
    async def run():
        client = FakeClient()
        callbacks = service(tmp_path, client)
        draft = await callbacks.prepare(PHONE, SUMMARY)
        client.user_error = CRMError("forbidden")
        with pytest.raises(CRMError, match="^forbidden$"):
            await callbacks.confirm(draft.id)
        assert (await callbacks.get(draft.id)).status == "draft"
        assert client.posts == []

    asyncio.run(run())


@pytest.mark.parametrize("identifier", ["invalid", str(uuid4()), None])
def test_unknown_operation_is_a_sanitized_error(tmp_path, identifier):
    async def run():
        callbacks = service(tmp_path)
        for method in (callbacks.get, callbacks.cancel, callbacks.confirm, callbacks.reconcile):
            with pytest.raises(CallbackError, match="^not_found$"):
                await method(identifier)

    asyncio.run(run())


def test_store_claim_is_atomic_across_threads_and_connections(tmp_path):
    callbacks = service(tmp_path)
    draft = asyncio.run(callbacks.prepare(PHONE, SUMMARY))

    def claim(_):
        another_store = CallbackStore(tmp_path / "callbacks.sqlite3")
        return another_store.transition(draft.id, expected={"draft"}, status="dispatching")[1]

    with ThreadPoolExecutor(max_workers=6) as executor:
        assert sum(executor.map(claim, range(12))) == 1
    saved = callbacks.store.get(draft.id)
    assert saved.status == "dispatching" and saved.summary == SUMMARY and saved.phone == PHONE


def test_storage_permissions_protect_new_paths_without_changing_existing_parent(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o755)
    existing.chmod(0o755)
    existing_database = existing / "callbacks.sqlite3"
    existing_database.touch(mode=0o644)
    CallbackStore(existing_database)
    assert stat.S_IMODE(existing.stat().st_mode) == 0o755
    assert stat.S_IMODE(existing_database.stat().st_mode) == 0o600
    new_database = existing / "private" / "nested" / "callbacks.sqlite3"
    CallbackStore(new_database)
    assert stat.S_IMODE(new_database.stat().st_mode) == 0o600
    assert stat.S_IMODE(new_database.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(new_database.parent.parent.stat().st_mode) == 0o700


def test_store_rejects_symlink_database_and_ephemeral_database(tmp_path):
    target = tmp_path / "untouched"
    target.touch(mode=0o644)
    target.chmod(0o644)
    linked = tmp_path / "linked.sqlite3"
    linked.symlink_to(target)
    with pytest.raises(CallbackStoreError, match="^storage_unavailable$"):
        CallbackStore(linked)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    with pytest.raises(CallbackStoreError, match="^storage_unavailable$"):
        CallbackStore(":memory:")


def test_deleted_store_is_not_silently_recreated_with_lost_operations(tmp_path):
    async def run():
        callbacks = service(tmp_path)
        draft = await callbacks.prepare(PHONE, SUMMARY)
        callbacks.store.path.unlink()
        with pytest.raises(CallbackError, match="^storage_unavailable$"):
            await callbacks.confirm(draft.id)
        assert not callbacks.store.path.exists()

    asyncio.run(run())


@pytest.mark.parametrize("status", ["draft", "canceled", "dispatching", "unconfirmed", "succeeded", "failed"])
def test_contact_name_migration_preserves_old_operations_and_claims(tmp_path, status):
    path = tmp_path / "callbacks.sqlite3"
    previous = CallbackOperation(
        id=str(uuid4()), status=status, phone=PHONE, summary=SUMMARY,
        contact_id="contact1", assigned_user_id=ASSIGNEE,
        task_id="task1" if status == "succeeded" else None,
    )
    payload_before = callback_task_payload(previous)
    # This is the original schema. An upgrade must not rearm a claimed UUID.
    with sqlite3.connect(path) as connection:
        connection.execute("""
            CREATE TABLE callback_operations (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, phone TEXT NOT NULL,
                summary TEXT NOT NULL, contact_id TEXT, assigned_user_id TEXT NOT NULL,
                task_id TEXT, error TEXT
            )
        """)
        connection.execute("""
            INSERT INTO callback_operations
            (id, status, phone, summary, contact_id, assigned_user_id, task_id, error)
            VALUES (:id, :status, :phone, :summary, :contact_id, :assigned_user_id, :task_id, :error)
        """, previous.model_dump(exclude={"contact_name"}))

    def migrate(_):
        return CallbackStore(path).get(previous.id)

    with ThreadPoolExecutor(max_workers=4) as executor:
        migrated = list(executor.map(migrate, range(8)))
    assert migrated == [previous] * 8
    assert all(operation.contact_name is None for operation in migrated)
    assert all(callback_task_payload(operation) == payload_before for operation in migrated)
    with sqlite3.connect(path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(callback_operations)")]
        assert columns.count("contact_name") == 1
        assert connection.execute("SELECT COUNT(*) FROM callback_operations").fetchone()[0] == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    crm = FakeClient([contact()])
    callbacks = CallbackService(crm, CallbackStore(path), ASSIGNEE)
    if status in ("canceled", "dispatching", "unconfirmed", "succeeded", "failed"):
        asyncio.run(callbacks.confirm(previous.id))
        assert crm.posts == []
    new_draft = asyncio.run(callbacks.prepare(PHONE, "New request"))
    assert CallbackStore(path).get(new_draft.id).contact_name == "Synthetic Contact"
