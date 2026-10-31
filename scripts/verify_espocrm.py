"""Verify local EspoCRM with clearly labelled synthetic records and restricted ACL."""

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import socket

import httpx

from voicecrm.config import Settings
from voicecrm.crm.client import CallCreate, CRMError, EspoCRMClient, TaskCreate


ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "runtime/verification.json"
PHONE = "+12025550100"


def save(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")


async def main() -> None:
    settings = Settings()
    bootstrap = json.loads((ROOT / "runtime/bootstrap.json").read_text())
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    password = (ROOT / "runtime/secrets/espo_admin_password").read_text().strip()
    authorization = base64.b64encode(f"admin:{password}".encode()).decode()
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as transport:
        crm = EspoCRMClient(settings, transport)
        await crm.probe()
        base = settings.espocrm_url.rstrip("/") + "/api/v1/"
        admin_headers = {"Espo-Authorization": authorization}
        if "contact_id" not in state:
            result = await transport.get(base + "Contact", headers=admin_headers, params={
                "maxSize": 2, "where[0][type]": "equals", "where[0][attribute]": "phoneNumberNumeric",
                "where[0][value]": PHONE[1:],
            })
            result.raise_for_status()
            matches = result.json()["list"]
            if len(matches) > 1:
                raise RuntimeError("Ambiguous synthetic contact")
            if matches:
                state["contact_id"] = matches[0]["id"]
            else:
                result = await transport.post(base + "Contact", headers=admin_headers, json={
                    "firstName": "Тест", "lastName": "VoiceCRM", "phoneNumber": "+1 (202) 555-0100",
                    "description": "Синтетический контакт для проверки интеграции. Это не клиент клиники.",
                })
                result.raise_for_status()
                state["contact_id"] = result.json()["id"]
            save(state)

        matches = await crm.search_contacts_by_phone(PHONE)
        if state["contact_id"] not in [record.id for record in matches]:
            raise RuntimeError("Formatted phone lookup did not return the synthetic contact")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        for key, create in (
            ("call_id", lambda: crm.create_call(CallCreate(
                name="[ТЕСТ] Проверка голосовой интеграции", assignedUserId=bootstrap["assigned_user_id"],
                description="Синтетическая API-проверка. Реального телефонного разговора не было.",
                parentType="Contact", parentId=state["contact_id"], dateStart=now - timedelta(minutes=2), dateEnd=now,
            ))),
            ("task_id", lambda: crm.create_task(TaskCreate(
                name="[ТЕСТ] Подключение голосового сервиса", assignedUserId=bootstrap["assigned_user_id"],
                description="Синтетическая задача для проверки CRM. Запись пациента не создавалась.",
                parentType="Contact", parentId=state["contact_id"],
            ))),
        ):
            if key not in state:
                if state.get(key + "_attempted"):
                    raise RuntimeError(f"Previous {key} write is unconfirmed; reconcile before repeating")
                state[key + "_attempted"] = True
                save(state)
                receipt = await create()
                state[key] = receipt.id
                save(state)

        api_headers = {"X-Api-Key": settings.espocrm_api_key.get_secret_value()}
        for entity, key in (("Call", "call_id"), ("Task", "task_id")):
            response = await transport.get(base + entity + "/" + state[key], headers=api_headers)
            response.raise_for_status()
            if response.json().get("parentId") != state["contact_id"]:
                raise RuntimeError("Activity did not retain its contact relationship")
        denied = await transport.delete(base + "Contact/" + state["contact_id"], headers=api_headers)
        if denied.status_code != 403:
            raise RuntimeError("Integration user unexpectedly has Contact delete access")
        denied = await transport.post(base + "Contact", headers=api_headers,
                                      json={"lastName": "[ТЕСТ] Запрещённое создание"})
        if denied.status_code != 403:
            raise RuntimeError("Integration user unexpectedly has Contact create access")
        for path in ("data/config.php", "data/config-internal.php", "application/Espo/Core/Application.php", "vendor/autoload.php"):
            protected = await transport.get(settings.espocrm_url.rstrip("/") + "/" + path)
            if protected.status_code not in (403, 404):
                raise RuntimeError("A private application path is accessible over HTTP")
        jobs = await transport.get(base + "ScheduledJobLogRecord", headers=admin_headers, params={
            "maxSize": 1, "select": "id,status", "where[0][type]": "equals",
            "where[0][attribute]": "status", "where[0][value]": "Success",
        })
        jobs.raise_for_status()
        if not jobs.json().get("list"):
            raise RuntimeError("No successful scheduled-job execution is recorded yet")
        state["scheduled_job_success_seen"] = True

        # This validates the transport handshake only, not authenticated notification delivery.
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        with socket.create_connection(("127.0.0.1", 8083), timeout=5) as connection:
            connection.sendall(("GET / HTTP/1.1\r\nHost: localhost:8083\r\nUpgrade: websocket\r\n"
                                "Connection: Upgrade\r\nOrigin: http://localhost:8080\r\n"
                                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                                "Sec-WebSocket-Protocol: wamp\r\n\r\n").encode())
            header = b""
            while b"\r\n\r\n" not in header and len(header) < 8192:
                chunk = connection.recv(1024)
                if not chunk:
                    break
                header += chunk
            text = header.decode("ascii", errors="replace")
            lines = text.split("\r\n")
            accept = next((line.split(":", 1)[1].strip() for line in lines
                           if line.lower().startswith("sec-websocket-accept:")), None)
            if not text.startswith("HTTP/1.1 101 ") or accept != expected:
                raise RuntimeError("WebSocket transport handshake did not succeed (" + lines[0] + ")")
        state["websocket_transport_handshake"] = True
        state["verified"] = True
        state["verified_at"] = now.isoformat()
        save(state)
    print("Verified: admin login, API user, formatted phone lookup, Call/Task round trip,")
    print("Contact create/delete denied, private application paths blocked, successful scheduled job seen,")
    print("WebSocket transport handshake HTTP101. Synthetic records labelled [ТЕСТ].")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except CRMError as error:
        raise SystemExit("CRM verification failed: " + error.code) from None
    except httpx.HTTPStatusError as error:
        raise SystemExit(f"CRM verification HTTP {error.response.status_code}") from None
    except (httpx.HTTPError, OSError):
        raise SystemExit("CRM verification transport failed") from None
    except (ValueError, KeyError, RuntimeError) as error:
        message = str(error) if isinstance(error, RuntimeError) else "Unexpected verification data"
        raise SystemExit(message) from None
