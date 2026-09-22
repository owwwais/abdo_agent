"""ويب هوك Hostinger Agentic Mail وTelegram.

Hostinger يرسل سر الويب هوك كـ«Authorization: Bearer <secret>» مع الحدث message.received. شكل الحمولة
غير موثق رسميًا، لذا نعامل الحدث كإشعار فقط: نتحقق من السر، نخزن الحدث (منع التكرار)، نرد سريعًا،
ثم تجلب مهمة المزامنة الرسائل الجديدة عبر IMAP بمؤشرها المحفوظ.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from app.auth.security import constant_time_equals
from app.db.models import Workspace
from app.jobs import queue
from app.services.channels import telegram_context
from app.services.inbound import record_webhook_event
from app.services.secrets import get_secret
from app.services.telegram_bot import handle_update
from app.workflows.common import Deps

router = APIRouter(prefix="/webhooks", include_in_schema=False)
MAX_BODY = 256 * 1024


async def _match_workspace(request: Request, secret_key: str, supplied: str) -> uuid.UUID | None:
    if not supplied:
        return None
    settings = request.app.state.settings
    async with request.app.state.sessionmaker() as db:
        for ws_id in (await db.execute(select(Workspace.id))).scalars():
            secret, _ = await get_secret(db, settings, ws_id, secret_key)
            if secret and constant_time_equals(supplied, secret):
                return ws_id
    return None


@router.post("/email/hostinger")
async def hostinger_email(request: Request) -> Response:
    body = await request.body()
    if len(body) > MAX_BODY:
        return JSONResponse({"error": {"code": "too_large"}}, status_code=413)
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    ws_id = await _match_workspace(request, "hostinger_webhook_secret", token)
    if ws_id is None:
        return JSONResponse(
            {"error": {"code": "unauthorized", "message": "سر الويب هوك غير صحيح"}}, status_code=401
        )
    try:
        payload: dict[str, Any] = json.loads(body) if body else {}
        if not isinstance(payload, dict):
            payload = {}
    except ValueError:
        payload = {}
    async with request.app.state.sessionmaker() as db, db.begin():
        new = await record_webhook_event(
            db, workspace_id=ws_id, provider="hostinger", body=body, payload=payload
        )
        if new:
            bucket = int(datetime.now(UTC).timestamp() // 30)
            await queue.enqueue(
                db,
                kind="mail_sync",
                workspace_id=ws_id,
                payload={"trigger": "webhook"},
                idempotency_key=f"{ws_id}:mailsync:hook:{bucket}",
            )
    return JSONResponse({"ok": True, "duplicate": not new})


@router.post("/telegram")
async def telegram_webhook(request: Request) -> Response:
    supplied = request.headers.get("x-telegram-bot-api-secret-token", "")
    ws_id = await _match_workspace(request, "telegram_webhook_secret", supplied)
    if ws_id is None:
        return JSONResponse({"error": {"code": "unauthorized"}}, status_code=401)
    body = await request.body()
    if len(body) > MAX_BODY:
        return JSONResponse({"ok": True})
    try:
        update = json.loads(body)
    except ValueError:
        return JSONResponse({"ok": True})
    deps = Deps(settings=request.app.state.settings, sm=request.app.state.sessionmaker)
    async with deps.sm() as db:
        tg = await telegram_context(db, deps.settings, ws_id)
    if tg.client is not None and isinstance(update, dict):
        await handle_update(deps, ws_id, update, tg.client, tg.config.chat_id)
    return JSONResponse({"ok": True})
