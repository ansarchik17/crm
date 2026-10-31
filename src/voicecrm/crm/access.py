"""Loopback and same-origin boundary for the local voice and CRM service."""

from fastapi import HTTPException, Request


def require_local(request: Request, *, json_body: bool = False) -> None:
    if (request.url.hostname not in ("localhost", "127.0.0.1", "::1", "testserver") or
            request.client is None or request.client.host not in
            ("localhost", "127.0.0.1", "::1", "testclient")):
        raise HTTPException(403, "Сервис доступен только локально.")
    origin = request.headers.get("origin")
    if ((origin and origin != str(request.base_url).rstrip("/")) or
            request.headers.get("sec-fetch-site") == "cross-site"):
        raise HTTPException(403, "Недопустимый источник запроса.")
    if json_body and request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(415, "Используйте Content-Type: application/json")
