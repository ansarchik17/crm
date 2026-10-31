"""Read-only callback reconciliation contracts; all HTTP is synthetic."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from voicecrm.crm.client import CRMError, EspoCRMClient


TASK_NAME = "Обратный звонок [VC:00000000-0000-4000-8000-000000000001]"
TASK = {
    "id": "task1", "name": TASK_NAME,
    "description": "Synthetic callback [VC:00000000-0000-4000-8000-000000000001]",
    "assignedUserId": "employee1", "parentType": "Contact", "parentId": "contact1",
}


def settings():
    return SimpleNamespace(espocrm_url="https://crm.example.test/nested/",
                           espocrm_api_key=SecretStr("synthetic-api-key"),
                           espocrm_timeout_seconds=1)


def test_get_user_reads_only_requested_id_and_discards_extra_user_data():
    seen = []

    def handle(request):
        seen.append(request)
        assert request.method == "GET"
        assert request.url.path == "/nested/api/v1/User/employee_1-2"
        assert dict(request.url.params) == {"select": "id"}
        return httpx.Response(200, json={"id": "employee_1-2", "name": "Synthetic User",
                                         "emailAddress": "unused@example.test", "isAdmin": True})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            receipt = await EspoCRMClient(settings(), http).get_user("employee_1-2")
            assert receipt.model_dump() == {"id": "employee_1-2"}

    asyncio.run(run())
    assert len(seen) == 1


@pytest.mark.parametrize("identifier", ["", "../Task", "employee/../../Task", "employee?select=password",
                                       "employee#fragment", "employee%2FTask", "employee\\Task",
                                       "employee\n", "employee name", "сотрудник", "x" * 101])
def test_get_user_rejects_invalid_path_identifiers_before_http(identifier):
    def handle(request):
        raise AssertionError("Invalid identifiers must not reach the transport")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with pytest.raises(CRMError, match="^invalid_response$") as error:
                await EspoCRMClient(settings(), http).get_user(identifier)
            assert error.value.__cause__ is None

    asyncio.run(run())


@pytest.mark.parametrize("payload", [{}, {"id": "anotherEmployee"}, {"id": ""}, {"id": 12},
                                    {"id": None}, {"id": "employee1/Task"}])
def test_get_user_requires_a_valid_receipt_for_the_exact_requested_user(payload):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload))) as http:
            with pytest.raises(CRMError, match="^invalid_response$") as error:
                await EspoCRMClient(settings(), http).get_user("employee1")
            assert error.value.__cause__ is None

    asyncio.run(run())


def test_find_callback_tasks_uses_bounded_exact_name_query_and_preserves_immutable_fields():
    seen = []

    def handle(request):
        seen.append(request)
        assert request.method == "GET"
        assert request.url.path == "/nested/api/v1/Task"
        assert dict(request.url.params) == {
            "maxSize": "2", "offset": "0",
            "select": "id,name,description,assignedUserId,parentType,parentId",
            "where[0][type]": "equals", "where[0][attribute]": "name",
            "where[0][value]": TASK_NAME,
        }
        return httpx.Response(200, json={"total": 1, "list": [{**TASK, "privateUnused": "ignored"}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            result = await EspoCRMClient(settings(), http).find_callback_tasks(TASK_NAME)
            assert len(result) == 1
            assert result[0].model_dump() == TASK

    asyncio.run(run())
    assert len(seen) == 1


def test_two_callback_task_matches_are_returned_for_service_to_detect_conflict():
    async def run():
        second = {**TASK, "id": "task2", "parentType": None, "parentId": None,
                  "assignedUserId": None, "description": None}
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"total": 2, "list": [TASK, second]}))) as http:
            records = await EspoCRMClient(settings(), http).find_callback_tasks(TASK_NAME)
            assert [record.id for record in records] == ["task1", "task2"]
            assert records[1].model_dump() == second

    asyncio.run(run())


@pytest.mark.parametrize("payload", [
    {"list": [], "total": 0}, {"list": []}, {"list": [], "total": None},
])
def test_zero_callback_matches_are_valid_read_results(payload):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload))) as http:
            assert await EspoCRMClient(settings(), http).find_callback_tasks(TASK_NAME) == []

    asyncio.run(run())


@pytest.mark.parametrize("payload", [
    {}, {"list": None}, {"list": {}}, {"list": "unexpected"},
    {"list": [TASK, TASK, TASK]},
    {"list": [], "total": 1}, {"list": [TASK], "total": 2},
    {"list": [TASK, {**TASK, "id": "task2"}], "total": 3},
    {"list": [], "total": True}, {"list": [], "total": False},
    {"list": [], "total": "0"}, {"list": [], "total": 0.0},
    {"list": [], "total": -1}, {"list": [], "total": []},
])
def test_invalid_or_truncated_callback_task_lists_never_become_complete_results(payload):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload))) as http:
            with pytest.raises(CRMError, match="^invalid_response$") as error:
                await EspoCRMClient(settings(), http).find_callback_tasks(TASK_NAME)
            assert error.value.__cause__ is None

    asyncio.run(run())


@pytest.mark.parametrize("record", [
    None, False, {}, {"id": "task1"}, {**TASK, "id": ""}, {**TASK, "id": 1},
    {**TASK, "id": "../Task"}, {**TASK, "name": False}, {**TASK, "name": "x" * 256},
    {**TASK, "description": {}}, {**TASK, "description": "x" * 10001},
    {**TASK, "assignedUserId": 1}, {**TASK, "assignedUserId": "x" * 101},
    {**TASK, "parentType": False}, {**TASK, "parentType": "x" * 101},
    {**TASK, "parentId": []}, {**TASK, "parentId": "x" * 101},
])
def test_invalid_callback_task_fields_are_rejected_instead_of_coerced(record):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"total": 1, "list": [record]}))) as http:
            with pytest.raises(CRMError, match="^invalid_response$") as error:
                await EspoCRMClient(settings(), http).find_callback_tasks(TASK_NAME)
            assert error.value.__cause__ is None

    asyncio.run(run())
