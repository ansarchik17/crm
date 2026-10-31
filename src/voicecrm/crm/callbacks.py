"""A confirmed callback task with one POST attempt per durable operation.

This deliberately trades automatic recovery for avoiding duplicate writes.
A crash after claiming a draft but before sending it can leave no CRM task;
GET reconciliation and, if necessary, manual review are then required.
"""

import asyncio
import re
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from voicecrm.crm.callback_store import CallbackOperation, CallbackStore, CallbackStoreError
from voicecrm.crm.client import CRMError, EspoCRMClient, RecordReceipt, TaskCreate, normalize_phone


class CallbackContact(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)
    id: str
    name: str | None = None


class CallbackError(Exception):
    """Fixed error code; contact choices are explicit, limited response data."""

    def __init__(self, code: str, *, contacts: list[CallbackContact] | None = None):
        self.code = code
        self.contacts = contacts or []
        super().__init__(code)


def callback_task_name(operation_id: str) -> str:
    return f"Обратный звонок [VC:{operation_id}]"


def callback_task_payload(operation: CallbackOperation) -> TaskCreate:
    return TaskCreate(
        name=("[ТЕСТ] " if operation.summary.startswith("[ТЕСТ]") else "") + callback_task_name(operation.id),
        assignedUserId=operation.assigned_user_id,
        description=(f"VoiceCRM operation: [VC:{operation.id}]\n"
                     f"Телефон: {operation.phone}\nПожелания: {operation.summary}"),
        parentType="Contact" if operation.contact_id else None,
        parentId=operation.contact_id,
    )


class CallbackService:
    def __init__(self, client: EspoCRMClient, store: CallbackStore, assignee_id: str):
        self.client = client
        self.store = store
        self.assignee_id = assignee_id.strip() if isinstance(assignee_id, str) else ""

    def _assignee(self) -> str:
        if not self.assignee_id:
            raise CallbackError("not_configured")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.assignee_id):
            raise CallbackError("invalid_assignee")
        return self.assignee_id

    @staticmethod
    async def _storage(method, *args, **kwargs):
        try:
            # Each store call opens and closes its own connection in the worker.
            # A busy SQLite writer must not stall other conversations. Canceling
            # this await cannot stop a running transaction: a committed claim
            # remains authoritative and must never be reset or sent again.
            return await asyncio.to_thread(method, *args, **kwargs)
        except CallbackStoreError:
            raise CallbackError("storage_unavailable") from None

    async def get(self, operation_id: str) -> CallbackOperation:
        try:
            if not isinstance(operation_id, str) or str(UUID(operation_id)) != operation_id:
                raise ValueError("Invalid identifier")
        except (ValueError, TypeError, AttributeError):
            raise CallbackError("not_found") from None
        operation = await self._storage(self.store.get, operation_id)
        if operation is None:
            raise CallbackError("not_found")
        return operation

    async def prepare(self, phone: str, summary: str, contact_id: str | None = None, *,
                      contact_selection_confirmed: bool = False) -> CallbackOperation:
        assignee_id = self._assignee()
        try:
            normalized = normalize_phone(phone)
        except ValueError:
            raise CallbackError("invalid_phone") from None
        if (not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 2000 or
                re.search(r"[\x00-\x08\x0b-\x1f\x7f]", summary)):
            raise CallbackError("invalid_summary")
        # Permission and contact reads happen before saving an actionable draft.
        user = await self.client.get_user(assignee_id)
        if user.id != assignee_id:
            raise CallbackError("invalid_assignee")
        contacts = await self.client.search_contacts_by_phone(normalized)
        choices = [CallbackContact(id=record.id, name=record.name) for record in contacts]
        # A model can supply a known candidate ID without the caller choosing it.
        # Only the separate human-selection route may assert this server flag.
        if len(contacts) >= 20 or (len(contacts) > 1 and
                                  (contact_id is None or contact_selection_confirmed is not True)):
            raise CallbackError("ambiguous_contact", contacts=choices)
        selected_id = contact_id
        if selected_id is not None and selected_id not in {record.id for record in contacts}:
            raise CallbackError("invalid_contact")
        if selected_id is None and len(contacts) == 1:
            selected_id = contacts[0].id
        selected_contact = next((record for record in contacts if record.id == selected_id), None)
        operation = CallbackOperation(
            id=str(uuid4()), status="draft", phone=normalized, summary=summary.strip(),
            contact_id=selected_id, contact_name=selected_contact.name if selected_contact else None,
            assigned_user_id=assignee_id,
        )
        return await self._storage(self.store.create, operation)

    async def cancel(self, operation_id: str) -> CallbackOperation:
        await self.get(operation_id)
        operation, _ = await self._storage(self.store.transition, operation_id, expected={"draft"}, status="canceled")
        return operation

    async def _finish(self, operation_id: str, *, status: str, task_id: str | None = None,
                      error: str | None = None) -> CallbackOperation:
        operation, _ = await self._storage(
            self.store.transition, operation_id, expected={"dispatching", "unconfirmed"},
            status=status, task_id=task_id, error=error,
        )
        if operation is None:
            raise CallbackError("not_found")
        return operation

    async def confirm(self, operation_id: str) -> CallbackOperation:
        operation = await self.get(operation_id)
        if operation.status != "draft":
            return await self.reconcile(operation_id)
        if self._assignee() != operation.assigned_user_id:
            raise CallbackError("invalid_assignee")
        # The preview can remain open while CRM contacts or access change.
        # Revalidate before claiming; a failed read leaves the draft untouched.
        user = await self.client.get_user(operation.assigned_user_id)
        if user.id != operation.assigned_user_id:
            raise CallbackError("invalid_assignee")
        contacts = await self.client.search_contacts_by_phone(operation.phone)
        choices = [CallbackContact(id=record.id, name=record.name) for record in contacts]
        if len(contacts) >= 20:
            raise CallbackError("ambiguous_contact", contacts=choices)
        if ((operation.contact_id is None and contacts) or
                (operation.contact_id is not None and operation.contact_id not in {record.id for record in contacts})):
            raise CallbackError("contact_changed", contacts=choices)
        operation, claimed = await self._storage(
            self.store.transition, operation_id, expected={"draft"}, status="dispatching",
        )
        if not claimed:
            return await self.reconcile(operation_id)
        try:
            receipt = await self.client.create_task(callback_task_payload(operation))
            receipt = RecordReceipt.model_validate(receipt)
        except asyncio.CancelledError:
            # If persistence fails, dispatching is already durable and still
            # blocks another POST. Preserve cancellation of the caller.
            try:
                await self._finish(operation_id, status="unconfirmed", error="write_unconfirmed")
            except CallbackError:
                pass
            raise
        except CRMError as error:
            return await self._finish(
                operation_id, status="unconfirmed" if error.code == "write_unconfirmed" else "failed",
                error=error.code,
            )
        except Exception:
            # An unexpected local/transport failure does not prove no write.
            return await self._finish(operation_id, status="unconfirmed", error="write_unconfirmed")
        return await self._finish(operation_id, status="succeeded", task_id=receipt.id)

    async def reconcile(self, operation_id: str) -> CallbackOperation:
        operation = await self.get(operation_id)
        if operation.status not in ("dispatching", "unconfirmed"):
            return operation
        expected = callback_task_payload(operation)
        try:
            records = await self.client.find_callback_tasks(expected.name)
        except asyncio.CancelledError:
            raise
        except CRMError as error:
            return await self._finish(operation_id, status="unconfirmed", error=error.code)
        except Exception:
            return await self._finish(operation_id, status="unconfirmed", error="unavailable")
        if not records:
            return await self._finish(operation_id, status="unconfirmed", error="write_unconfirmed")
        if len(records) == 1:
            record = records[0]
            if all(getattr(record, field, None) == getattr(expected, field) for field in (
                "name", "description", "assignedUserId", "parentType", "parentId",
            )):
                return await self._finish(operation_id, status="succeeded", task_id=record.id)
        return await self._finish(operation_id, status="unconfirmed", error="reconciliation_conflict")
