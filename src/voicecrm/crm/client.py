"""Server-only EspoCRM REST adapter. Timed-out writes may already be committed."""

import asyncio
import re
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from voicecrm.config import Settings

ErrorCode = Literal["not_configured", "authentication", "forbidden", "conflict",
                    "unavailable", "invalid_response", "write_unconfirmed"]


class CRMError(Exception):
    """Fixed error code only; never an upstream response, phone or credential."""

    def __init__(self, code: ErrorCode):
        self.code = code
        super().__init__(code)


def normalize_phone(phone: str) -> str:
    """Require an international number instead of guessing the caller's country."""
    message = "Нужен телефон в международном формате: + и 7–15 цифр."
    if not isinstance(phone, str) or len(phone) > 64 or not phone.strip().startswith("+"):
        raise ValueError(message)
    if re.search(r"[^0-9+ ()\-.]", phone) or phone.count("+") != 1:
        raise ValueError(message)
    normalized = re.sub(r"[ ()\-.]", "", phone.strip())
    if not re.fullmatch(r"\+[1-9][0-9]{6,14}", normalized):
        raise ValueError(message)
    return normalized


class ContactSummary(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    name: str | None = Field(default=None, max_length=500)
    phoneNumber: str | None = Field(default=None, max_length=100)
    emailAddress: str | None = Field(default=None, max_length=320)


class RecordReceipt(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class CallbackTaskRecord(RecordReceipt):
    name: str = Field(max_length=255)
    description: str | None = Field(default=None, max_length=10000)
    assignedUserId: str | None = Field(default=None, max_length=100)
    parentType: str | None = Field(default=None, max_length=100)
    parentId: str | None = Field(default=None, max_length=100)


class _ActivityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=255)
    assignedUserId: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    description: str | None = Field(default=None, max_length=10000)
    parentType: Literal["Account", "Contact", "Lead", "Opportunity", "Case"] | None = None
    parentId: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")

    @model_validator(mode="after")
    def require_complete_parent(self):
        if (self.parentType is None) != (self.parentId is None):
            raise ValueError("parentType и parentId должны быть заданы вместе.")
        return self


class CallCreate(_ActivityCreate):
    dateStart: datetime
    dateEnd: datetime
    direction: Literal["Inbound", "Outbound"] = "Inbound"
    status: Literal["Planned", "Held", "Not Held"] = "Held"

    @field_validator("dateStart", "dateEnd")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Дата должна содержать часовой пояс.")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.dateEnd < self.dateStart:
            raise ValueError("dateEnd должен быть не раньше dateStart.")
        return self


class TaskCreate(_ActivityCreate):
    status: Literal["Not Started", "Started", "Completed", "Canceled", "Deferred"] = "Not Started"
    priority: Literal["Low", "Normal", "High", "Urgent"] = "Normal"


class EspoCRMClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient):
        self.settings = settings
        self.http_client = http_client

    @property
    def configured(self) -> bool:
        return bool(self.settings.espocrm_url.strip() and self.settings.espocrm_api_key.get_secret_value())

    def _api_url(self, entity: str) -> str:
        site = self.settings.espocrm_url.rstrip("/")
        try:
            parsed = urlsplit(site)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname or
                    parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError("Invalid site URL")
            if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1", "espocrm"):
                raise ValueError("HTTP is local only")
        except ValueError:
            raise CRMError("not_configured") from None
        return site + "/api/v1/" + entity

    async def _request(self, method: Literal["GET", "POST"], entity: str,
                       *, params: dict[str, str | int] | None = None, payload: dict | None = None) -> dict:
        if not self.configured:
            raise CRMError("not_configured")
        url = self._api_url(entity)
        try:
            async with asyncio.timeout(self.settings.espocrm_timeout_seconds):
                response = await self.http_client.request(
                    method, url, params=params, json=payload,
                    headers={"X-Api-Key": self.settings.espocrm_api_key.get_secret_value(),
                             "Accept": "application/json", "Content-Type": "application/json"},
                    timeout=self.settings.espocrm_timeout_seconds, follow_redirects=False,
                )
                if response.status_code in (401, 403, 409):
                    code: ErrorCode = {401: "authentication", 403: "forbidden", 409: "conflict"}[response.status_code]
                    raise CRMError(code)
                if response.status_code not in (200, 201):
                    code = "write_unconfirmed" if method == "POST" and response.status_code >= 500 else "unavailable"
                    raise CRMError(code)
                if len(response.content) > 512 * 1024:
                    raise ValueError("Oversized response")
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError("Unexpected response")
                return result
        except (httpx.HTTPError, httpx.InvalidURL, TimeoutError):
            raise CRMError("write_unconfirmed" if method == "POST" else "unavailable") from None
        except (ValueError, TypeError):
            raise CRMError("write_unconfirmed" if method == "POST" else "invalid_response") from None

    async def probe(self) -> None:
        result = await self._request("GET", "Contact", params={"maxSize": 1, "select": "id"})
        records = result.get("list")
        if not isinstance(records, list) or len(records) > 1:
            raise CRMError("invalid_response")
        try:
            for record in records:
                RecordReceipt.model_validate(record)
        except ValidationError:
            raise CRMError("invalid_response") from None

    async def get_user(self, user_id: str) -> RecordReceipt:
        """Check the single server-configured assignee without disclosing user fields."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", user_id):
            raise CRMError("invalid_response")
        result = await self._request("GET", "User/" + user_id, params={"select": "id"})
        try:
            receipt = RecordReceipt.model_validate(result)
            if receipt.id != user_id:
                raise ValueError("Unexpected user")
            return receipt
        except (ValidationError, ValueError):
            raise CRMError("invalid_response") from None

    async def find_callback_tasks(self, name: str) -> list[CallbackTaskRecord]:
        """Read-only reconciliation. A missing match never authorizes another POST."""
        result = await self._request("GET", "Task", params={
            "maxSize": 2, "offset": 0,
            "select": "id,name,description,assignedUserId,parentType,parentId",
            "where[0][type]": "equals", "where[0][attribute]": "name",
            "where[0][value]": name,
        })
        records = result.get("list")
        total = result.get("total")
        if (not isinstance(records, list) or len(records) > 2 or
                isinstance(total, bool) or (total is not None and (not isinstance(total, int) or total < 0)) or
                (isinstance(total, int) and total > len(records))):
            raise CRMError("invalid_response")
        try:
            return [CallbackTaskRecord.model_validate(record) for record in records]
        except ValidationError:
            raise CRMError("invalid_response") from None

    async def search_contacts_by_phone(self, phone: str) -> list[ContactSummary]:
        number = normalize_phone(phone)
        result = await self._request("GET", "Contact", params={
            "maxSize": 20, "offset": 0, "select": "id,name,phoneNumber,emailAddress",
            # EspoCRM 10.0.9 stores digits separately and applies this filter to
            # all related numbers. Formatting of the saved number is preserved.
            "where[0][type]": "equals", "where[0][attribute]": "phoneNumberNumeric",
            "where[0][value]": number[1:],
        })
        records = result.get("list")
        if not isinstance(records, list) or len(records) > 20:
            raise CRMError("invalid_response")
        try:
            return [ContactSummary.model_validate(record) for record in records]
        except ValidationError:
            raise CRMError("invalid_response") from None

    async def _create(self, entity: Literal["Call", "Task"], activity: _ActivityCreate) -> RecordReceipt:
        payload = activity.model_dump(exclude_none=True)
        for field in ("dateStart", "dateEnd"):
            if field in payload:
                payload[field] = payload[field].strftime("%Y-%m-%d %H:%M:%S")
        result = await self._request("POST", entity, payload=payload)
        try:
            return RecordReceipt.model_validate(result)
        except ValidationError:
            raise CRMError("write_unconfirmed") from None

    async def create_call(self, call: CallCreate) -> RecordReceipt:
        """Record an actual call after confirmation; not a telephone operation."""
        return await self._create("Call", call)

    async def create_task(self, task: TaskCreate) -> RecordReceipt:
        """Create one task; caller must implement durable delivery/idempotency."""
        return await self._create("Task", task)
