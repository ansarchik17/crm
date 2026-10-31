"""Opt-in synthetic local callback verification; never blindly retries a POST.

Run with --synthetic-bootstrap-assignee to explicitly use the assignee recorded
by the earlier synthetic CRM check. This does not configure production routing.
--with-openai additionally checks one real model tool request (billable).
The persisted operation is reused on later runs, including after a timeout.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

import httpx
from fastapi.testclient import TestClient

from voicecrm.config import Settings
from voicecrm.crm.callbacks import callback_task_payload
from voicecrm.crm.client import EspoCRMClient
from voicecrm.main import create_app

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "runtime/callback-verification.json"
DATABASE = ROOT / "runtime/callback-verification.sqlite3"
SUMMARY = "[ТЕСТ] Проверка обратного звонка. Вымышленное обращение, реальному клиенту не звонить."
PHONE = "+12025550199"


def checked(response, expected: int = 200) -> dict:
    if response.status_code != expected:
        raise RuntimeError(f"Callback verification HTTP {response.status_code}; response body hidden")
    result = response.json()
    if not isinstance(result, dict):
        raise RuntimeError("Unexpected callback response")
    return result


def save_operation(operation_id: str) -> None:
    temporary = STATE.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump({"operation_id": operation_id}, stream)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, STATE)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic-bootstrap-assignee", action="store_true")
    parser.add_argument("--with-openai", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    assignee = settings.callback_assignee_id
    if args.synthetic_bootstrap_assignee:
        bootstrap = json.loads((ROOT / "runtime/bootstrap.json").read_text())
        assignee = bootstrap.get("assigned_user_id")
    if not assignee:
        raise RuntimeError("Configure CALLBACK_ASSIGNEE_ID or explicitly select the synthetic bootstrap assignee")
    settings = settings.model_copy(update={"callback_assignee_id": assignee,
                                            "callback_store_path": DATABASE})
    if not args.with_openai:
        settings = settings.model_copy(update={"llm_mode": "demo"})

    app = create_app(settings)
    with TestClient(app) as client:
        if STATE.exists():
            operation_id = json.loads(STATE.read_text())["operation_id"]
            if str(UUID(operation_id)) != operation_id:
                raise RuntimeError("Invalid verification state; inspect manually before continuing")
            operation = checked(client.get(f"/crm/callbacks/{operation_id}"))["callback"]
        else:
            if args.with_openai:
                session_id = checked(client.post("/sessions"), 201)["session_id"]
                result = checked(client.post(f"/sessions/{session_id}/messages", json={
                    "text": f"Прошу подготовить заявку на обратный звонок. Телефон {PHONE}. "
                            f"Суть обращения передай точно: {SUMMARY} Это синтетическая проверка, только черновик.",
                }))
                operation = result.get("callback")
                if not operation or operation.get("status") != "draft":
                    raise RuntimeError("The model did not prepare a callback draft; no CRM task was requested")
            else:
                operation = checked(client.post("/crm/callbacks", json={"phone": PHONE, "summary": SUMMARY}), 201)["callback"]
            if not operation["summary"].startswith("[ТЕСТ]"):
                raise RuntimeError("Synthetic marker missing; refusing confirmation")
            operation_id = operation["id"]
            # Persist before confirmation: a lost response never starts a new operation.
            save_operation(operation_id)
        if operation["phone"] != PHONE or not operation["summary"].startswith("[ТЕСТ]"):
            raise RuntimeError("Verification state is not synthetic; refusing to continue")
        result = checked(client.post(f"/crm/callbacks/{operation_id}/confirm", json={}))
        operation = result["callback"]
        if operation["status"] != "succeeded":
            operation = checked(client.post(f"/crm/callbacks/{operation_id}/reconcile", json={}))["callback"]
        if operation["status"] != "succeeded" or not operation["task_id"]:
            raise RuntimeError("Callback remains unconfirmed; inspect existing CRM records, do not create another operation")
        repeated = checked(client.post(f"/crm/callbacks/{operation_id}/confirm", json={}))["callback"]
        if repeated != operation:
            raise RuntimeError("Repeated confirmation changed the persisted result")
        persisted = asyncio.run(app.state.callbacks.get(operation_id))
        expected = callback_task_payload(persisted)

        async def read_back():
            async with httpx.AsyncClient() as http:
                records = await EspoCRMClient(settings, http).find_callback_tasks(expected.name)
            if len(records) != 1 or records[0].id != operation["task_id"]:
                raise RuntimeError("Expected exactly one synthetic CRM task")
            if not all(getattr(records[0], field) == getattr(expected, field) for field in
                       ("name", "description", "assignedUserId", "parentType", "parentId")):
                raise RuntimeError("CRM task fields do not match the confirmed draft")

        asyncio.run(read_back())
    print("Synthetic callback verified: one CRM Task, matching fields, repeated confirmation reuses the same receipt.")
    print("Operation ID preserved locally; subsequent runs only reuse or reconcile this operation. No credentials or personal data printed.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Library/provider exceptions may contain private request data.
        raise SystemExit("Callback verification failed. Inspect persisted verification status locally; do not repeat an unknown write with a new operation.") from None
