from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from voicecrm.config import Settings
from voicecrm.main import create_app
from voicecrm.dialogue.sessions import MAX_SESSIONS


class Providers:
    def __init__(self):
        self.calls = []

    async def transcribe(self, audio):
        self.calls.append("stt")
        return "Синтетический текст"

    async def create_call(self, sdp):
        self.calls.append("realtime")
        return sdp


@pytest.mark.parametrize("route", ["sessions", "delete", "messages", "stt", "realtime", "config"])
@pytest.mark.parametrize("boundary", ["origin", "host", "cross_site", "remote_peer"])
def test_voice_endpoints_reject_nonlocal_requests_before_providers(route, boundary):
    providers = Providers()
    app = create_app(Settings(_env_file=None, llm_mode="demo"),
                     recognizer=providers, realtime=providers)
    headers = {"origin": "https://outside.example"} if boundary == "origin" else {
        "host": "outside.example"} if boundary == "host" else {
        "sec-fetch-site": "cross-site"} if boundary == "cross_site" else {}
    peer = ("203.0.113.8", 12345) if boundary == "remote_peer" else ("testclient", 12345)
    with TestClient(app, client=peer) as client:
        if route == "sessions":
            response = client.post("/sessions", headers=headers)
        elif route == "delete":
            response = client.delete(f"/sessions/{uuid4()}", headers=headers)
        elif route == "messages":
            response = client.post(f"/sessions/{uuid4()}/messages", headers=headers, json={"text": "Тест"})
        elif route == "stt":
            response = client.post("/transcriptions", headers={**headers, "content-type": "audio/wav"}, content=b"invalid")
        elif route == "realtime":
            response = client.post("/realtime/calls", headers={**headers, "content-type": "application/sdp"},
                                   content="v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n")
        else:
            response = client.get("/realtime/config", headers=headers)
        assert response.status_code == 403
        assert providers.calls == []


def test_foreign_session_requests_cannot_consume_capacity_or_delete_context():
    with TestClient(create_app(Settings(_env_file=None, llm_mode="demo"))) as client:
        foreign = {"origin": "https://outside.example"}
        assert client.post("/sessions", headers=foreign).status_code == 403
        sessions = [client.post("/sessions", headers={"origin": "http://testserver"})
                    for _ in range(MAX_SESSIONS)]
        assert all(response.status_code == 201 for response in sessions)
        sid = sessions[0].json()["session_id"]
        assert client.delete(f"/sessions/{sid}", headers=foreign).status_code == 403
        assert client.post(f"/sessions/{sid}/messages", json={"text": "Здравствуйте"}).status_code == 200
        assert client.post("/sessions").status_code == 429
        assert client.delete(f"/sessions/{sid}").status_code == 204
        assert client.post("/sessions").status_code == 201
