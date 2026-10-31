"""Real SQLite contention with synthetic CRM; no network or runtime data."""

import asyncio
import sqlite3
import threading
from time import monotonic
from types import SimpleNamespace

import pytest

from voicecrm.crm.callback_store import CallbackStore
from voicecrm.crm.callbacks import CallbackService
from voicecrm.crm.client import RecordReceipt


PHONE = "+12025550199"
SUMMARY = "Synthetic callback concurrency check"
ASSIGNEE = "synthetic_employee"


class SyntheticCRM:
    def __init__(self):
        self.posts = []
        self.tasks = []

    async def get_user(self, user_id):
        return RecordReceipt(id=user_id)

    async def search_contacts_by_phone(self, phone):
        return []

    async def create_task(self, task):
        self.posts.append(task)
        self.tasks.append(SimpleNamespace(id="synthetic_task", **task.model_dump()))
        return RecordReceipt(id="synthetic_task")

    async def find_callback_tasks(self, name):
        return [task for task in self.tasks if task.name == name]


class ObservedStore(CallbackStore):
    """Observe the real claim transaction without replacing its SQLite work."""

    def __init__(self, path):
        super().__init__(path)
        self.claim_started = threading.Event()
        self.claim_finished = threading.Event()

    def transition(self, *args, **kwargs):
        claiming = kwargs.get("status") == "dispatching"
        if claiming:
            self.claim_started.set()
        try:
            return super().transition(*args, **kwargs)
        finally:
            if claiming:
                self.claim_finished.set()


async def wait_for_thread_event(event):
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, 2), timeout=3)


def test_write_lock_does_not_block_other_async_work(tmp_path):
    async def run():
        store = CallbackStore(tmp_path / "callbacks.sqlite3")
        callbacks = CallbackService(SyntheticCRM(), store, ASSIGNEE)
        blocker = sqlite3.connect(store.path, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        started = monotonic()
        preparing = asyncio.create_task(callbacks.prepare(PHONE, SUMMARY))
        try:
            # This coroutine must run while SQLite waits on an independent
            # writer. The old synchronous call stalled it for five seconds.
            await asyncio.sleep(0.05)
            assert monotonic() - started < 1
            assert not preparing.done()
        finally:
            blocker.rollback()
            blocker.close()
            results = await asyncio.gather(preparing, return_exceptions=True)
        assert not isinstance(results[0], BaseException)
        assert results[0].status == "draft"

    asyncio.run(run())


def test_contending_confirmations_across_connections_send_one_post(tmp_path):
    async def run():
        path = tmp_path / "callbacks.sqlite3"
        first_store, second_store = ObservedStore(path), ObservedStore(path)
        crm = SyntheticCRM()
        first = CallbackService(crm, first_store, ASSIGNEE)
        second = CallbackService(crm, second_store, ASSIGNEE)
        draft = await first.prepare(PHONE, SUMMARY)
        blocker = sqlite3.connect(path, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        confirming = [asyncio.create_task(callbacks.confirm(draft.id)) for callbacks in (first, second)]
        try:
            await wait_for_thread_event(first_store.claim_started)
            await wait_for_thread_event(second_store.claim_started)
            assert crm.posts == []
            assert all(not task.done() for task in confirming)
        finally:
            blocker.rollback()
            blocker.close()
            results = await asyncio.gather(*confirming, return_exceptions=True)
        assert all(not isinstance(result, BaseException) for result in results)
        assert len(crm.posts) == 1
        restarted = CallbackService(crm, CallbackStore(path), ASSIGNEE)
        result = await restarted.confirm(draft.id)
        assert result.status == "succeeded"
        assert result.task_id == "synthetic_task"
        assert len(crm.posts) == 1

    asyncio.run(run())


def test_cancel_while_claim_waits_preserves_late_commit_without_reposting(tmp_path):
    async def run():
        path = tmp_path / "callbacks.sqlite3"
        store = ObservedStore(path)
        crm = SyntheticCRM()
        callbacks = CallbackService(crm, store, ASSIGNEE)
        draft = await callbacks.prepare(PHONE, SUMMARY)
        blocker = sqlite3.connect(path, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        confirming = asyncio.create_task(callbacks.confirm(draft.id))
        try:
            await wait_for_thread_event(store.claim_started)
            confirming.cancel()
            with pytest.raises(asyncio.CancelledError):
                await confirming
            assert not store.claim_finished.is_set()
            assert crm.posts == []
        finally:
            blocker.rollback()
            blocker.close()
            if not confirming.done():
                confirming.cancel()
            await asyncio.gather(confirming, return_exceptions=True)
        # Canceling the coroutine does not stop a running SQLite thread. Its
        # durable claim must still block all later attempts, even after restart.
        await wait_for_thread_event(store.claim_finished)
        restarted = CallbackService(crm, CallbackStore(path), ASSIGNEE)
        assert (await restarted.get(draft.id)).status == "dispatching"
        for _ in range(2):
            assert (await restarted.confirm(draft.id)).status == "unconfirmed"
        assert crm.posts == []

    asyncio.run(run())
