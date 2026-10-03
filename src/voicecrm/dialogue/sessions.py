"""In-memory text sessions for the local prototype. Not call identifiers: see telephony."""

import asyncio
from dataclasses import dataclass, field
from uuid import UUID, uuid4

MAX_SESSIONS = 100
MAX_MESSAGES = 40  # 20 client turns plus 20 replies


@dataclass
class Session:
    messages: list[dict[str, str]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionStore:
    def __init__(self):
        self._sessions: dict[UUID, Session] = {}

    def __len__(self) -> int:
        return len(self._sessions)

    def create(self) -> UUID | None:
        if len(self._sessions) >= MAX_SESSIONS:
            return None
        session_id = uuid4()
        self._sessions[session_id] = Session()
        return session_id

    def get(self, session_id: UUID) -> Session | None:
        return self._sessions.get(session_id)

    def delete(self, session_id: UUID) -> None:
        del self._sessions[session_id]

    def clear(self) -> None:
        self._sessions.clear()
