"""In-memory text sessions for the local prototype. Not call identifiers: see telephony."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from uuid import UUID, uuid4

MAX_SESSIONS = 100
MAX_MESSAGES = 40  # 20 client turns plus 20 replies


@dataclass
class Session:
    messages: list[dict[str, str]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    callback_id: str | None = None
    callback_context: dict | None = None
    # A validated draft supersedes earlier dialogue; retain only later wishes.
    callback_history_start: int = 0
    # The UI may keep sending a terminal receipt while a new request unfolds.
    callback_terminal_id: str | None = None
    last_seen: float = 0


class SessionStore:
    def __init__(self, *, idle_timeout_seconds: float = 1800, clock: Callable[[], float] = monotonic):
        if idle_timeout_seconds <= 0:
            raise ValueError("Session timeout must be positive")
        self._sessions: dict[UUID, Session] = {}
        self.idle_timeout_seconds = idle_timeout_seconds
        self._clock = clock

    def _expire(self, now: float) -> None:
        # In-flight work owns its session until it finishes, even after the TTL.
        for session_id, session in list(self._sessions.items()):
            if not session.lock.locked() and now - session.last_seen >= self.idle_timeout_seconds:
                del self._sessions[session_id]

    def __len__(self) -> int:
        self._expire(self._clock())
        return len(self._sessions)

    def create(self) -> UUID | None:
        now = self._clock()
        self._expire(now)
        if len(self._sessions) >= MAX_SESSIONS:
            return None
        session_id = uuid4()
        self._sessions[session_id] = Session(last_seen=now)
        return session_id

    def get(self, session_id: UUID) -> Session | None:
        now = self._clock()
        self._expire(now)
        session = self._sessions.get(session_id)
        if session is not None:
            session.last_seen = now
        return session

    def delete(self, session_id: UUID) -> None:
        del self._sessions[session_id]

    def clear(self) -> None:
        self._sessions.clear()
