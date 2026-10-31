import asyncio
import json
from email.parser import BytesParser
from email.policy import default

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from voicecrm.config import Settings
from voicecrm.main import create_app
from voicecrm.dialogue.realtime import MAX_SDP_BYTES, RealtimeError, RealtimeGateway, session_config

SDP = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


def test_realtime_config_missing_key_and_page_do_not_require_alem():
    with TestClient(create_app(settings())) as client:
        assert client.get("/realtime").status_code == 200
        config = client.get("/realtime/config").json()
        assert config["configured"] is False
        assert config["silence_duration_ms"] == 300
        assert config["test_duration_seconds"] == 300
        response = client.post("/realtime/calls", content=SDP, headers={"Content-Type": "application/sdp"})
        assert response.status_code == 503
        assert "OPENAI_API_KEY" in response.json()["detail"]


@pytest.mark.parametrize("kind", ["success", "auth", "quota", "bad_request", "unavailable", "timeout", "malformed"])
def test_realtime_gateway_contract_and_sanitized_errors(kind):
    config = settings(openai_api_key="test-secret")

    def handle(request):
        assert str(request.url) == "https://api.openai.com/v1/realtime/calls"
        assert request.headers["Authorization"] == "Bearer test-secret"
        # Real multipart parsing verifies content types and separates the SDP from config.
        message = BytesParser(policy=default).parsebytes(
            ("Content-Type: " + request.headers["Content-Type"] + "\r\n\r\n").encode() + request.content)
        fields = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                  for part in message.iter_parts()}
        assert fields["sdp"].decode() == SDP
        session = json.loads(fields["session"])
        assert session["type"] == "realtime"
        assert session["output_modalities"] == ["audio"]
        assert session["reasoning"]["effort"] == "minimal"
        assert session["audio"]["input"]["turn_detection"]["silence_duration_ms"] == 300
        assert session["audio"]["input"]["turn_detection"]["interrupt_response"] is True
        assert "стоматологии" in session["instructions"]
        if kind == "timeout":
            raise httpx.ReadTimeout("test-secret", request=request)
        codes = {"auth": 401, "quota": 429, "bad_request": 400, "unavailable": 500}
        return httpx.Response(codes.get(kind, 201), text=SDP if kind == "success" else "test-secret")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            gateway = RealtimeGateway(config, client)
            if kind == "success":
                assert await gateway.create_call(SDP) == SDP
            else:
                with pytest.raises(RealtimeError) as error:
                    await gateway.create_call(SDP)
                assert "test-secret" not in str(error.value)
                assert error.value.status == (504 if kind == "timeout" else 503 if kind == "quota" else 502)
    asyncio.run(run())


@pytest.mark.parametrize("silence_ms", [300, 500])
def test_realtime_silence_env_setting_reaches_public_config_and_openai(monkeypatch, silence_ms):
    monkeypatch.setenv("OPENAI_REALTIME_SILENCE_MS", str(silence_ms))
    config = settings(openai_api_key="test-secret")

    with TestClient(create_app(config, realtime=FakeGateway())) as client:
        assert client.get("/realtime/config").json()["silence_duration_ms"] == silence_ms

    def handle(request):
        message = BytesParser(policy=default).parsebytes(
            ("Content-Type: " + request.headers["Content-Type"] + "\r\n\r\n").encode() + request.content)
        fields = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                  for part in message.iter_parts()}
        session = json.loads(fields["session"])
        assert fields["sdp"].decode() == SDP
        assert session["audio"]["input"]["turn_detection"]["silence_duration_ms"] == silence_ms
        assert session["reasoning"]["effort"] == "minimal"
        return httpx.Response(201, text=SDP)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            assert await RealtimeGateway(config, client).create_call(SDP) == SDP

    asyncio.run(run())


@pytest.mark.parametrize("silence_ms", [99, 1001])
def test_realtime_silence_setting_rejects_values_outside_prototype_bounds(monkeypatch, silence_ms):
    monkeypatch.setenv("OPENAI_REALTIME_SILENCE_MS", str(silence_ms))
    with pytest.raises(ValidationError) as error:
        settings()
    assert error.value.errors()[0]["loc"] == ("openai_realtime_silence_ms",)


class FakeGateway:
    def __init__(self):
        self.calls = []

    async def create_call(self, sdp):
        self.calls.append(sdp)
        return SDP


@pytest.mark.parametrize("body,headers,expected", [
    (SDP, {"Content-Type": "text/plain"}, 415),
    ("bad", {"Content-Type": "application/sdp"}, 422),
    (b"\xff", {"Content-Type": "application/sdp"}, 422),
    (SDP + "x" * MAX_SDP_BYTES, {"Content-Type": "application/sdp"}, 413),
    (SDP, {"Content-Type": "application/sdp", "Origin": "https://other.example"}, 403),
    (SDP, {"Content-Type": "application/sdp", "Host": "other.example"}, 403),
    (SDP, {"Content-Type": "application/sdp", "Sec-Fetch-Site": "cross-site"}, 403),
])
def test_realtime_rejects_invalid_or_cross_origin_requests_before_provider(body, headers, expected):
    gateway = FakeGateway()
    with TestClient(create_app(settings(), realtime=gateway)) as client:
        assert client.post("/realtime/calls", content=body, headers=headers).status_code == expected
    assert gateway.calls == []


def test_realtime_route_returns_only_sdp_and_public_config():
    gateway = FakeGateway()
    with TestClient(create_app(settings(openai_api_key="test-secret"), realtime=gateway)) as client:
        config = client.get("/realtime/config")
        assert config.json()["configured"] is True
        assert "test-secret" not in config.text
        response = client.post("/realtime/calls", content=SDP,
                               headers={"Content-Type": "application/sdp", "Origin": "http://testserver"})
        assert response.status_code == 200
        assert response.text == SDP
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-type"].startswith("application/sdp")
        assert gateway.calls == [SDP]


def test_non_reasoning_model_does_not_receive_reasoning_parameter():
    assert "reasoning" not in session_config(settings(openai_realtime_model="gpt-realtime-1.5"))
