"""Explicit local confirmation of immutable callback drafts; models cannot confirm."""

import asyncio
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from voicecrm.crm.access import require_local
from voicecrm.crm.callbacks import CallbackError
from voicecrm.crm.client import CRMError
from voicecrm.dialogue.sessions import SessionStore


class CallbackDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=2000)
    contact_id: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    session_id: UUID | None = None
    previous_id: UUID | None = None


class CallbackAction(BaseModel):
    model_config = ConfigDict(extra="forbid")


def callback_public(operation) -> dict:
    return {field: getattr(operation, field) for field in
            ("id", "status", "phone", "summary", "contact_id", "contact_name", "task_id", "error")}


def callback_model_context(operation) -> dict:
    """Validated draft data, without confirmation or contact-selection authority."""
    return {"draft_id": operation.id, **{field: getattr(operation, field) for field in
            ("status", "phone", "summary", "contact_id", "contact_name")}}


async def cancel_callback_for_edit(service, session, operation):
    """Keep a durable local cancellation and its session context in agreement."""
    if session is None:
        return await service.cancel(operation.id)
    pending = asyncio.create_task(service.cancel(operation.id))
    interrupted = False
    while True:
        try:
            canceled = await asyncio.shield(pending)
            break
        except asyncio.CancelledError:
            if pending.cancelled():
                raise
            # SQLite cannot cancel a running transaction. Finish this bounded
            # local operation under the session lock before releasing it.
            interrupted = True
        except Exception:
            if interrupted:
                raise asyncio.CancelledError from None
            raise
    if canceled.status == "canceled":
        if not session.callback_context or session.callback_context.get("draft_id") != canceled.id:
            session.callback_history_start = len(session.messages)
        session.callback_context = callback_model_context(canceled)
        session.callback_terminal_id = canceled.id
    if interrupted:
        raise asyncio.CancelledError
    return canceled


def callback_text(operation) -> str:
    if operation.status == "draft":
        contact = (f"Контакт: {operation.contact_name}." if operation.contact_name else
                   "Контакт выбран; имя недоступно." if operation.contact_id else
                   "Без привязки к контакту.")
        return (f"Подготовлено обращение: {operation.summary} Телефон: {operation.phone}. "
                f"{contact} "
                "Проверьте данные и нажмите «Подтвердить заявку» или скажите «Подтверждаю заявку».")
    return {
        "succeeded": "Задача на обратный звонок создана в CRM.",
        "canceled": "Черновик обращения отменён.",
        "failed": "Не удалось создать задачу в CRM. Обращение не подтверждено.",
        "dispatching": "Обращение отправляется. Результат CRM пока не подтверждён.",
        "unconfirmed": "CRM пока не подтвердила результат. Нажмите «Проверить результат». Повторно отправлять обращение не нужно.",
    }[operation.status]


def callback_service(request: Request):
    require_local(request, json_body=request.method == "POST")
    service = request.app.state.callbacks
    if service is None:
        raise HTTPException(503, "Обращения пока не настроены: нужен ответственный сотрудник CRM.")
    return service


def callback_exception(error: CallbackError | CRMError) -> HTTPException:
    code = error.code
    message = {
        "not_configured": "Обращения пока не настроены: нужен ответственный сотрудник CRM.",
        "not_found": "Обращение не найдено.",
        "ambiguous_contact": "Найдено несколько контактов. Уточните клиента перед подтверждением.",
        "invalid_contact": "Выбранный контакт не соответствует телефону.",
        "invalid_assignee": "Ответственный сотрудник недоступен.",
        "invalid_phone": "Укажите телефон в международном формате.",
        "invalid_summary": "Укажите содержание обращения.",
        "contact_changed": "Контакты изменились. Подготовьте и подтвердите новый черновик.",
    }.get(code, "CRM недоступна или результат операции не подтверждён.")
    status = 404 if code == "not_found" else 409 if code == "ambiguous_contact" else 503 if code == "not_configured" else 422 if code.startswith("invalid_") and code != "invalid_response" else 502
    contacts = [c.model_dump() if hasattr(c, "model_dump") else c for c in getattr(error, "contacts", [])]
    detail = {"message": message, "code": code, "contacts": contacts} if contacts or code == "not_found" else message
    return HTTPException(status, detail)


def callback_response(operation) -> JSONResponse:
    return JSONResponse({"callback": callback_public(operation), "text": callback_text(operation)},
                        headers={"Cache-Control": "no-store"})


def build_callback_router(sessions: SessionStore) -> APIRouter:
    router = APIRouter(prefix="/crm/callbacks", tags=["Обращения"])

    @router.post("", status_code=201)
    async def prepare(draft: CallbackDraft, request: Request):
        service = callback_service(request)
        session = sessions.get(draft.session_id) if draft.session_id else None
        if draft.session_id is not None and session is None:
            raise HTTPException(404, "Сессия закончилась. Начните новый диалог.")
        if session is not None and session.lock.locked():
            raise HTTPException(409, "Дождитесь завершения текущего ответа.")

        async def prepare_draft():
            previous_id = session.callback_id if session and session.callback_id else str(draft.previous_id) if draft.previous_id else None
            if previous_id:
                previous = await service.get(previous_id)
                if previous.status in ("dispatching", "unconfirmed"):
                    raise HTTPException(409, "Сначала проверьте результат отправленного обращения.")
                if previous.status == "draft":
                    canceled = await cancel_callback_for_edit(service, session, previous)
                    if canceled.status != "canceled":
                        raise HTTPException(409, "Прежнее обращение уже отправляется. Сначала проверьте его результат.")
            operation = await service.prepare(
                **draft.model_dump(exclude={"session_id", "previous_id"}),
                contact_selection_confirmed=bool(draft.contact_id),
            )
            if session is not None:
                session.callback_id = operation.id
                session.callback_context = callback_model_context(operation)
                session.callback_history_start = len(session.messages)
                session.callback_terminal_id = None
            return operation

        try:
            if session is None:
                operation = await prepare_draft()
            else:
                async with session.lock:
                    try:
                        operation = await prepare_draft()
                    finally:
                        sessions.get(draft.session_id)
        except (CallbackError, CRMError) as error:
            raise callback_exception(error) from None
        response = callback_response(operation)
        response.status_code = 201
        return response

    @router.get("/{operation_id}")
    async def get(operation_id: UUID, request: Request):
        service = callback_service(request)
        try:
            return callback_response(await service.get(str(operation_id)))
        except (CallbackError, CRMError) as error:
            raise callback_exception(error) from None

    @router.post("/{operation_id}/{action}")
    async def act(operation_id: UUID, action: str, body: CallbackAction, request: Request):
        service = callback_service(request)
        if action not in ("confirm", "cancel", "reconcile"):
            raise HTTPException(404, "Действие не найдено.")
        try:
            return callback_response(await getattr(service, action)(str(operation_id)))
        except (CallbackError, CRMError) as error:
            raise callback_exception(error) from None

    return router
