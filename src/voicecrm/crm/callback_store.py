"""Durable callback drafts and a committed claim before any CRM write.

Only status, receipt and fixed error codes can change after a draft is saved.
A claimed operation is never made eligible for another POST, including after
a crash between the local commit and the first network request.
"""

import os
import re
import sqlite3
import stat
from collections.abc import Collection
from contextlib import closing
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

CallbackStatus = Literal["draft", "canceled", "dispatching", "unconfirmed", "succeeded", "failed"]
_STATUSES = frozenset(("draft", "canceled", "dispatching", "unconfirmed", "succeeded", "failed"))
_TRANSITIONS = {
    "draft": frozenset(("canceled", "dispatching")),
    "dispatching": frozenset(("unconfirmed", "succeeded", "failed")),
    "unconfirmed": frozenset(("unconfirmed", "succeeded", "failed")),
    "canceled": frozenset(), "succeeded": frozenset(), "failed": frozenset(),
}


class CallbackOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str
    status: CallbackStatus
    phone: str = Field(pattern=r"^\+[1-9][0-9]{6,14}$", repr=False)
    summary: str = Field(min_length=1, max_length=2000, repr=False)
    contact_id: str | None = Field(default=None, min_length=1, max_length=100,
                                   pattern=r"^[A-Za-z0-9_-]+$", repr=False)
    contact_name: str | None = Field(default=None, max_length=500, repr=False)
    assigned_user_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$", repr=False)
    task_id: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    error: str | None = Field(default=None, max_length=64, pattern=r"^[a-z_]+$")

    @field_validator("id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("Invalid operation identifier")
        return value


class CallbackStoreError(Exception):
    """A fixed message, without filesystem paths or operation contents."""

    def __init__(self):
        super().__init__("storage_unavailable")


class CallbackStore:
    def __init__(self, path: str | Path):
        try:
            self.path = Path(path)
            if not str(path) or str(path) == ":memory:":
                raise ValueError("A persistent database path is required")
            # Do not change permissions of directories that already exist.
            missing: list[Path] = []
            parent = self.path.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                try:
                    directory.mkdir(mode=0o700)
                    directory.chmod(0o700)
                except FileExistsError:
                    if not directory.is_dir():
                        raise ValueError("Invalid database directory") from None
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError("A regular database file is required")
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            with closing(self._connect()) as connection:
                # Serialize startup migrations across processes as well as writes.
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS callback_operations (
                        id TEXT PRIMARY KEY,
                        status TEXT NOT NULL CHECK (status IN
                            ('draft', 'canceled', 'dispatching', 'unconfirmed', 'succeeded', 'failed')),
                        phone TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        contact_id TEXT,
                        contact_name TEXT,
                        assigned_user_id TEXT NOT NULL,
                        task_id TEXT,
                        error TEXT
                    )
                """)
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(callback_operations)")}
                if "contact_name" not in columns:
                    # NULL preserves old immutable payloads and reconciliation.
                    connection.execute("ALTER TABLE callback_operations ADD COLUMN contact_name TEXT")
                connection.commit()
        except (OSError, sqlite3.Error, TypeError, ValueError):
            raise CallbackStoreError() from None

    def _connect(self) -> sqlite3.Connection:
        # mode=rw prevents an accidental fresh/insecure database after deletion.
        connection = sqlite3.connect(self.path.absolute().as_uri() + "?mode=rw", uri=True,
                                     timeout=5, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA synchronous = FULL")
        except BaseException:
            connection.close()
            raise
        return connection

    @staticmethod
    def _operation(row: sqlite3.Row | None) -> CallbackOperation | None:
        return CallbackOperation.model_validate(dict(row)) if row is not None else None

    def create(self, operation: CallbackOperation) -> CallbackOperation:
        try:
            if operation.status != "draft" or operation.task_id is not None or operation.error is not None:
                raise ValueError("Only a new draft can be stored")
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("""
                    INSERT INTO callback_operations
                    (id, status, phone, summary, contact_id, contact_name, assigned_user_id, task_id, error)
                    VALUES (:id, :status, :phone, :summary, :contact_id, :contact_name, :assigned_user_id, :task_id, :error)
                """, operation.model_dump())
                connection.commit()
            return operation
        except (sqlite3.Error, ValueError, TypeError):
            raise CallbackStoreError() from None

    def get(self, operation_id: str) -> CallbackOperation | None:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute("SELECT * FROM callback_operations WHERE id = ?", (operation_id,)).fetchone()
                return self._operation(row)
        except (sqlite3.Error, ValidationError, ValueError, TypeError):
            raise CallbackStoreError() from None

    def transition(self, operation_id: str, *, expected: Collection[CallbackStatus],
                   status: CallbackStatus, task_id: str | None = None,
                   error: str | None = None) -> tuple[CallbackOperation | None, bool]:
        """Compare and set under an exclusive writer transaction, then commit.

        Return the current persisted state and whether this call changed it.
        In particular, a late reconciliation cannot regress a succeeded result.
        """
        try:
            if (status not in _STATUSES or not expected or not set(expected) <= _STATUSES or
                    (error is not None and not re.fullmatch(r"[a-z_]{1,64}", error))):
                raise ValueError("Invalid transition")
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM callback_operations WHERE id = ?", (operation_id,)).fetchone()
                current = self._operation(row)
                changed = current is not None and current.status in expected
                if changed:
                    if status not in _TRANSITIONS[current.status]:
                        raise ValueError("A dispatched draft cannot be rearmed")
                    # Validate before persisting; the immutable payload is never updated.
                    updated = CallbackOperation.model_validate({
                        **current.model_dump(), "status": status, "task_id": task_id, "error": error,
                    })
                    connection.execute("""
                        UPDATE callback_operations SET status = ?, task_id = ?, error = ? WHERE id = ?
                    """, (updated.status, updated.task_id, updated.error, operation_id))
                    current = updated
                connection.commit()
            return current, changed
        except (sqlite3.Error, ValidationError, ValueError, TypeError):
            raise CallbackStoreError() from None
