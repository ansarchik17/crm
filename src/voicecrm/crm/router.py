"""Local read-only CRM diagnostics. No public record-creation endpoints."""

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from voicecrm.config import Settings
from voicecrm.crm.client import CRMError, EspoCRMClient, normalize_phone


def build_crm_router(client: EspoCRMClient | None, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/crm", tags=["CRM"])

    def local_client(request: Request) -> EspoCRMClient:
        if (request.url.hostname not in ("localhost", "127.0.0.1", "::1", "testserver") or
                request.client is None or request.client.host not in ("localhost", "127.0.0.1", "::1", "testclient")):
            raise HTTPException(403, "CRM-диагностика доступна только локально.")
        origin = request.headers.get("origin")
        if (origin and origin != str(request.base_url).rstrip("/")) or request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Недопустимый источник запроса.")
        return client if client is not None else request.app.state.crm

    @router.get("/status")
    async def status(request: Request):
        crm = local_client(request)
        configured = crm.configured
        reason = None
        connected = False
        if configured:
            try:
                await crm.probe()
                connected = True
            except CRMError as error:
                reason = error.code
        else:
            reason = "not_configured"
        return JSONResponse({"provider": "espocrm", "configured": configured,
                             "connected": connected, "reason": reason},
                            headers={"Cache-Control": "no-store"})

    @router.get("/contacts")
    async def contacts(request: Request, phone: str = Query(min_length=1, max_length=64)):
        crm = local_client(request)
        try:
            normalized = normalize_phone(phone)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        try:
            records = await crm.search_contacts_by_phone(normalized)
        except CRMError as error:
            code = 503 if error.code == "not_configured" else 502
            raise HTTPException(code, "EspoCRM недоступна или доступ не настроен.") from None
        return JSONResponse({"list": [record.model_dump() for record in records],
                             "count": len(records), "limit": 20},
                            headers={"Cache-Control": "no-store"})

    return router
