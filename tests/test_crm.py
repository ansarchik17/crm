import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from voicecrm.crm.client import CRMError, CallCreate, EspoCRMClient, TaskCreate, normalize_phone
from voicecrm.crm.router import build_crm_router


def settings(**overrides):
    values = {"espocrm_url": "http://127.0.0.1:8080", "espocrm_api_key": SecretStr("test-crm-secret"),
              "espocrm_timeout_seconds": 1}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_search_contract_matches_digits_and_preserves_formatted_contacts_and_all_matches():
    def handle(request):
        assert request.method == "GET"
        assert request.url.path == "/crm/api/v1/Contact"
        assert request.headers["X-Api-Key"] == "test-crm-secret"
        assert "Authorization" not in request.headers
        assert dict(request.url.params) == {
            "maxSize": "20", "offset": "0", "select": "id,name,phoneNumber,emailAddress",
            "where[0][type]": "equals", "where[0][attribute]": "phoneNumberNumeric",
            "where[0][value]": "15555550123",
        }
        return httpx.Response(200, json={"total": 2, "list": [
            {"id": "contact1", "name": "Synthetic One", "phoneNumber": "+1 (555) 555-0123",
             "description": "private field"},
            {"id": "contact2", "name": "Synthetic Two"},
        ]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            crm = EspoCRMClient(settings(espocrm_url="https://crm.example/crm/"), http)
            matches = await crm.search_contacts_by_phone("+1 (555) 555-0123")
            assert [record.id for record in matches] == ["contact1", "contact2"]
            assert matches[0].phoneNumber == "+1 (555) 555-0123"
            assert "description" not in matches[0].model_dump()

    asyncio.run(run())


def test_create_contracts_use_confirmed_ids_and_utc_dates_without_retries():
    requests = []
    start = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=3)))

    def handle(request):
        assert request.method == "POST"
        assert request.headers["X-Api-Key"] == "test-crm-secret"
        assert "X-Skip-Duplicate-Check" not in request.headers
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"id": "record1", "internal": "unused"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            crm = EspoCRMClient(settings(), http)
            receipt = await crm.create_call(CallCreate(name=" Inbound call ", assignedUserId="employee1",
                                                      dateStart=start, dateEnd=start + timedelta(minutes=1),
                                                      parentType="Contact", parentId="contact1"))
            assert receipt.model_dump() == {"id": "record1"}
            await crm.create_task(TaskCreate(name="Follow up", assignedUserId="employee1"))

    asyncio.run(run())
    assert requests == [
        ("/api/v1/Call", {"name": "Inbound call", "assignedUserId": "employee1",
                          "parentType": "Contact", "parentId": "contact1",
                          "dateStart": "2026-01-01 09:00:00", "dateEnd": "2026-01-01 09:01:00",
                          "direction": "Inbound", "status": "Held"}),
        ("/api/v1/Task", {"name": "Follow up", "assignedUserId": "employee1",
                          "status": "Not Started", "priority": "Normal"}),
    ]


@pytest.mark.parametrize("kind,code", [(401, "authentication"), (403, "forbidden"),
                                      (409, "conflict"), (500, "write_unconfirmed"),
                                      ("timeout", "write_unconfirmed"), ("malformed", "write_unconfirmed"),
                                      ("missing_id", "write_unconfirmed"), ("empty_id", "write_unconfirmed")])
def test_failed_or_uncertain_write_is_sanitized_and_not_retried(kind, code):
    calls = []

    def handle(request):
        calls.append(request)
        if isinstance(kind, int):
            return httpx.Response(kind, text="test-crm-secret private upstream detail")
        if kind == "timeout":
            raise httpx.ReadTimeout("test-crm-secret", request=request)
        if kind == "malformed":
            return httpx.Response(200, text="test-crm-secret")
        return httpx.Response(200, json={} if kind == "missing_id" else {"id": ""})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with pytest.raises(CRMError) as error:
                await EspoCRMClient(settings(), http).create_task(TaskCreate(name="Synthetic", assignedUserId="employee1"))
            assert error.value.code == code
            assert str(error.value) == code
            assert error.value.__cause__ is None

    asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize("url", ["http://public.example", "https://user:password@crm.example",
                                 "https://crm.example?token=private", "https://crm.example#fragment"])
def test_invalid_configuration_never_sends_a_key(url):
    def handle(request):
        raise AssertionError("No request is allowed")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with pytest.raises(CRMError, match="^not_configured$"):
                await EspoCRMClient(settings(espocrm_url=url), http).probe()

    asyncio.run(run())


def test_redirect_never_forwards_credentials_to_another_site():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example/collect"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True) as http:
            with pytest.raises(CRMError, match="^unavailable$"):
                await EspoCRMClient(settings(), http).probe()

    asyncio.run(run())
    assert len(calls) == 1


def test_whole_request_deadline_and_unconfigured_client():
    async def stalled(request):
        await asyncio.Event().wait()
        raise AssertionError("Must be cancelled")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(stalled)) as http:
            with pytest.raises(CRMError, match="^unavailable$"):
                await EspoCRMClient(settings(espocrm_timeout_seconds=0.01), http).probe()
            with pytest.raises(CRMError, match="^not_configured$"):
                await EspoCRMClient(settings(espocrm_api_key=SecretStr("")), http).probe()

    asyncio.run(run())


@pytest.mark.parametrize("payload", [{}, {"list": None}, {"list": [False]},
                                     {"list": [{"id": 42}]}, {"list": [{"id": ""}]},
                                     {"list": [{"id": "record1", "name": False}]}])
def test_malformed_contact_results_never_become_successful_matches(payload):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload))) as http:
            with pytest.raises(CRMError, match="^invalid_response$"):
                await EspoCRMClient(settings(), http).search_contacts_by_phone("+15555550123")

    asyncio.run(run())


@pytest.mark.parametrize("phone", ["15555550123", "+01234567890", "+1", "+15555550123 ext 2", "+1+5555550123"])
def test_phone_country_and_extensions_are_not_guessed(phone):
    with pytest.raises(ValueError, match="международном формате"):
        normalize_phone(phone)


def test_write_validation_rejects_arbitrary_fields_and_incomplete_actions():
    for payload in ({"name": "Task"}, {"name": "  ", "assignedUserId": "employee1"},
                    {"name": "Task", "assignedUserId": "employee1", "parentId": "contact1"},
                    {"name": "Task", "assignedUserId": "employee1", "isAdmin": True}):
        with pytest.raises(ValidationError):
            TaskCreate(**payload)
    with pytest.raises(ValidationError, match="часовой пояс"):
        CallCreate(name="Call", assignedUserId="employee1", dateStart=datetime(2026, 1, 1),
                   dateEnd=datetime(2026, 1, 1, 0, 1))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValidationError, match="не раньше"):
        CallCreate(name="Call", assignedUserId="employee1", dateStart=now, dateEnd=now - timedelta(seconds=1))


def make_app(crm, config):
    app = FastAPI()
    app.include_router(build_crm_router(crm, config))
    return app


def test_router_status_is_a_real_read_probe_and_does_not_expose_records():
    seen = []

    def handle(request):
        seen.append(request)
        assert dict(request.url.params) == {"maxSize": "1", "select": "id"}
        return httpx.Response(200, json={"list": [{"id": "contact1"}], "total": 1})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            config = settings()
            with TestClient(make_app(EspoCRMClient(config, http), config)) as browser:
                response = browser.get("/crm/status")
                assert response.json() == {"provider": "espocrm", "configured": True,
                                           "connected": True, "reason": None}
                assert response.headers["Cache-Control"] == "no-store"

    asyncio.run(run())
    assert len(seen) == 1


def test_router_handles_forbidden_contacts_empty_matches_and_invalid_input():
    requests = []

    def handle(request):
        requests.append(request)
        if "where[0][type]" in request.url.params:
            return httpx.Response(200, json={"list": [], "total": 0})
        return httpx.Response(403, text="test-crm-secret upstream details")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            config = settings()
            with TestClient(make_app(EspoCRMClient(config, http), config)) as browser:
                status = browser.get("/crm/status")
                assert status.json()["reason"] == "forbidden"
                assert status.json()["connected"] is False
                response = browser.get("/crm/contacts", params={"phone": "+15555550123"})
                assert response.json() == {"list": [], "count": 0, "limit": 20}
                assert response.headers["Cache-Control"] == "no-store"
                assert browser.get("/crm/contacts", params={"phone": "555"}).status_code == 422
                assert browser.post("/crm/contacts").status_code == 405
                assert browser.post("/crm/tasks").status_code == 404

    asyncio.run(run())
    assert len(requests) == 2


@pytest.mark.parametrize("headers", [{"Host": "public.example"}, {"Origin": "https://other.example"},
                                   {"Sec-Fetch-Site": "cross-site"}])
def test_router_rejects_remote_host_and_cross_site_browser_requests(headers):
    def handle(request):
        raise AssertionError("Must not contact CRM")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            config = settings()
            with TestClient(make_app(EspoCRMClient(config, http), config)) as browser:
                assert browser.get("/crm/status", headers=headers).status_code == 403
                assert browser.get("/crm/contacts", params={"phone": "+15555550123"}, headers=headers).status_code == 403

    asyncio.run(run())


def test_router_rejects_remote_peer_even_if_host_is_forged():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as http:
            config = settings()
            with TestClient(make_app(EspoCRMClient(config, http), config), client=("198.51.100.1", 5000)) as browser:
                assert browser.get("/crm/status", headers={"Host": "localhost"}).status_code == 403

    asyncio.run(run())
