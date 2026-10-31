"""Configure a restricted local EspoCRM API user without printing credentials."""

import base64
import copy
import json
from pathlib import Path

from dotenv import dotenv_values, set_key
import httpx


ROOT = Path(__file__).resolve().parents[1]
SITE = "http://localhost:8080"
ROLE_NAME = "VoiceCRM Integration"
USER_NAME = "voicecrm-api"


def request(client: httpx.Client, method: str, path: str, **kwargs) -> dict:
    response = client.request(method, path, **kwargs)
    if response.status_code not in (200, 201):
        raise RuntimeError(f"EspoCRM bootstrap: {method} {path} HTTP {response.status_code}")
    result = response.json()
    if not isinstance(result, dict):
        raise RuntimeError("EspoCRM returned an unexpected response")
    return result


def find(client: httpx.Client, entity: str, field: str, value: str) -> dict | None:
    result = request(client, "GET", entity, params={
        "maxSize": 2, "where[0][type]": "equals",
        "where[0][attribute]": field, "where[0][value]": value,
    })
    records = result.get("list", [])
    if len(records) > 1:
        raise RuntimeError(f"Ambiguous bootstrap {entity}; refusing to choose one")
    return records[0] if records else None


def main() -> None:
    password = (ROOT / "runtime/secrets/espo_admin_password").read_text().strip()
    authorization = base64.b64encode(f"admin:{password}".encode()).decode()
    with httpx.Client(base_url=SITE + "/api/v1/", timeout=30, follow_redirects=False,
                      headers={"Espo-Authorization": authorization}) as client:
        scopes = request(client, "GET", "Metadata", params={"key": "scopes"})
        if any(not isinstance(scopes.get(scope), dict) or not scopes[scope].get("acl")
               for scope in ("Contact", "Call", "Task")):
            raise RuntimeError("EspoCRM metadata does not expose the required ACL scopes")
        data: dict = {scope: False for scope, definition in scopes.items()
                      if isinstance(definition, dict) and definition.get("acl")}
        data["Contact"] = {"create": "no", "read": "all", "edit": "no", "delete": "no", "stream": "no"}
        for scope in ("Call", "Task"):
            data[scope] = {"create": "yes", "read": "all", "edit": "no", "delete": "no", "stream": "no"}
        # EspoCRM validates assignedUserId through read access to the foreign User.
        data["User"] = {"read": "all", "edit": "no"}
        user_fields = request(client, "GET", "Metadata", params={"key": "entityDefs.User.fields"})
        field_data = {"User": {field: {"read": "no", "edit": "no"}
                               for field in user_fields if field not in ("id", "name", "userName")}}
        role_payload = {"name": ROLE_NAME, "data": data, "fieldData": field_data,
                        "assignmentPermission": "all", "userPermission": "no",
                        "userCalendarPermission": "no", "exportPermission": "no",
                        "massUpdatePermission": "no", "dataPrivacyPermission": "no"}
        role = find(client, "Role", "name", ROLE_NAME)
        if role is None:
            role = request(client, "POST", "Role", json=role_payload)
        else:
            role = request(client, "GET", "Role/" + role["id"])
            if any(role.get(key) != value for key, value in role_payload.items()):
                previous = copy.deepcopy(role_payload)
                previous["data"]["User"] = False
                previous["fieldData"] = {}
                if any(role.get(key) != value for key, value in previous.items()):
                    raise RuntimeError("Existing integration role differs; refusing to widen or overwrite it")
                role = request(client, "PUT", "Role/" + role["id"], json=role_payload)

        user = find(client, "User", "userName", USER_NAME)
        if user is None:
            user = request(client, "POST", "User", json={
                "userName": USER_NAME, "type": "api", "authMethod": "ApiKey",
                "isActive": True, "rolesIds": [role["id"]],
            })
        else:
            user = request(client, "GET", "User/" + user["id"])
        if user.get("type") != "api" or user.get("authMethod") != "ApiKey" or not user.get("isActive"):
            raise RuntimeError("Existing API user differs; refusing to modify it")
        if user.get("rolesIds") != [role["id"]]:
            raise RuntimeError("API user must have only the restricted integration role")

        env_path = ROOT / ".env.espocrm"
        existing = dotenv_values(env_path) if env_path.exists() else {}
        api_key = existing.get("ESPOCRM_API_KEY")
        if not api_key:
            generated = request(client, "POST", "UserSecurity/apiKey/generate", json={"id": user["id"]})
            api_key = generated.get("apiKey")
        if not isinstance(api_key, str) or not api_key:
            raise RuntimeError("EspoCRM did not return a usable API key")

        with httpx.Client(timeout=15, headers={"X-Api-Key": api_key}) as probe:
            response = probe.get(SITE + "/api/v1/Contact", params={"maxSize": 1, "select": "id"})
            if response.status_code != 200:
                raise RuntimeError(f"API user probe HTTP {response.status_code}; credentials were not exported")

        if not env_path.exists():
            env_path.touch(mode=0o600)
        env_path.chmod(0o600)
        set_key(str(env_path), "ESPOCRM_URL", SITE)
        set_key(str(env_path), "ESPOCRM_API_KEY", api_key)
        set_key(str(env_path), "ESPOCRM_TIMEOUT_SECONDS", "10")
        admin = find(client, "User", "userName", "admin")
        state = {"site": SITE, "role_id": role["id"], "api_user_id": user["id"],
                 "assigned_user_id": admin["id"] if admin else None}
        (ROOT / "runtime/bootstrap.json").write_text(json.dumps(state, indent=2) + "\n")
    print("EspoCRM integration role and API user ready; authenticated Contact probe succeeded.")
    print("Voice-service configuration saved to ignored .env.espocrm; no credentials printed.")


if __name__ == "__main__":
    try:
        main()
    except (httpx.HTTPError, ValueError, RuntimeError) as error:
        # Only our fixed diagnostics: upstream bodies and headers may contain keys.
        if isinstance(error, RuntimeError):
            raise SystemExit(str(error)) from None
        raise SystemExit("EspoCRM bootstrap failed: transport or unexpected response") from None
