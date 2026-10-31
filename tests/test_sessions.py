import asyncio

import pytest

from voicecrm.dialogue.sessions import MAX_SESSIONS, SessionStore


def test_idle_expiry_refresh_and_capacity_are_based_on_activity():
    now = [0.0]
    store = SessionStore(idle_timeout_seconds=10, clock=lambda: now[0])
    first, second = store.create(), store.create()
    now[0] = 8
    assert store.get(first) is not None
    now[0] = 10
    assert store.get(second) is None
    assert store.get(first) is not None
    now[0] = 20
    assert store.get(first) is None
    assert len(store) == 0
    assert all(store.create() is not None for _ in range(MAX_SESSIONS))
    assert store.create() is None
    now[0] = 30
    assert store.create() is not None
    assert len(store) == 1


def test_locked_session_survives_idle_expiry_until_the_operation_finishes():
    async def run():
        now = [0.0]
        store = SessionStore(idle_timeout_seconds=10, clock=lambda: now[0])
        sid = store.create()
        active = store.get(sid)
        async with active.lock:
            now[0] = 100
            assert len(store) == 1
            assert store.get(sid) is active
        now[0] = 109
        assert len(store) == 1
        now[0] = 110
        assert len(store) == 0

    asyncio.run(run())


@pytest.mark.parametrize("timeout", [0, -1])
def test_nonpositive_idle_timeout_is_rejected(timeout):
    with pytest.raises(ValueError):
        SessionStore(idle_timeout_seconds=timeout)
