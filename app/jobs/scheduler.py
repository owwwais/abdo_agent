"""المجدول (SPEC §9.1): يضع المهام المستحقة في الطابور بمفاتيح فريدة تمنع التكرار.

- لا يعمل إلا لـworkspace بوضع التشغيل الفعلي (live). وضع demo يشغّل يدويًا من اللوحة فقط.
- دورة الاكتشاف ونوافذ المعالجة والملخص تُجدول داخل «نافذة سماح» بعد موعدها؛ ما فات بعدها لا يُعوّض
  دفعة واحدة، ويُسجل كتشغيل مُتخطى مرة واحدة.
- مزامنة البريد كل N دقائق، والتنظيف مرة يوميًا.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import Job, Workspace
from app.db.models_ops import Run
from app.jobs import queue
from app.services import integrations as integ


def _at(day: Any, hhmm: str, tz: ZoneInfo) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day, time(h, m), tzinfo=tz)


async def _due(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    kind: str,
    key: str,
    hhmm: str,
    payload: dict[str, Any],
    local: datetime,
    tz: ZoneInfo,
    grace: timedelta,
    now: datetime,
    created: list[str],
) -> None:
    """يضع المهمة إن كنا داخل نافذة السماح؛ بعدها يسجل التخطي مرة واحدة ولا يعوّض."""
    start = _at(local.date(), hhmm, tz)
    if start <= local < start + grace:
        job = await queue.enqueue(
            db, kind=kind, workspace_id=ws_id, payload=payload, idempotency_key=key
        )
        created.append(f"{kind}:{job.id}")
    elif local >= start + grace:
        marker = f"{key}:missed"
        exists = (
            await db.execute(select(Job).where(Job.idempotency_key.in_((key, marker))))
        ).first()
        if exists is None:
            await queue.enqueue(
                db, kind="noop", workspace_id=ws_id, payload={"missed": key}, idempotency_key=marker
            )
            if kind == "discover_daily":
                db.add(
                    Run(
                        workspace_id=ws_id,
                        kind="discover",
                        status="canceled",
                        ended_at=now,
                        summary={"reason": f"فاتت نافذة {hhmm}؛ لا تعويض لأيام أو نوافذ فائتة"},
                    )
                )


async def schedule_due(
    sm: async_sessionmaker[AsyncSession], settings: Settings, now: datetime | None = None
) -> list[str]:
    now = now or datetime.now(UTC)
    created: list[str] = []
    async with sm() as db:
        workspaces = list((await db.execute(select(Workspace))).scalars())
    for ws in workspaces:
        async with sm() as db, db.begin():
            ops = await integ.get_config(db, settings, ws.id, "operations", integ.OperationsConfig)
            mail = await integ.get_config(db, settings, ws.id, "mail", integ.MailConfig)
            tz = ZoneInfo(ws.timezone)
            local = now.astimezone(tz)
            day = local.date()
            grace = timedelta(hours=ops.grace_hours)

            common: dict[str, Any] = {
                "ws_id": ws.id,
                "local": local,
                "tz": tz,
                "grace": grace,
                "now": now,
                "created": created,
            }
            if ops.operating_mode == "live" and not ops.pause_all_processing:
                if not ops.pause_discovery:
                    await _due(
                        db,
                        kind="discover_daily",
                        key=f"{ws.id}:discover:{day}",
                        hhmm=ops.discovery_time,
                        payload={},
                        **common,
                    )
                for slot in ops.process_slots:
                    await _due(
                        db,
                        kind="process_window",
                        key=f"{ws.id}:process:{day}:{slot}",
                        hhmm=slot,
                        payload={"slot": slot},
                        **common,
                    )
                await _due(
                    db,
                    kind="digest",
                    key=f"{ws.id}:digest:{day}",
                    hhmm=ops.digest_time,
                    payload={},
                    **common,
                )
            if mail.provider in ("smtp", "hostinger_api") and mail.imap_enabled:
                bucket = int(now.timestamp() // (mail.sync_interval_minutes * 60))
                job = await queue.enqueue(
                    db,
                    kind="mail_sync",
                    workspace_id=ws.id,
                    payload={},
                    idempotency_key=f"{ws.id}:mailsync:{bucket}",
                )
                created.append(f"mail_sync:{job.id}")
    async with sm() as db, db.begin():
        await queue.enqueue(
            db,
            kind="cleanup",
            workspace_id=None,
            payload={},
            idempotency_key=f"cleanup:{now.astimezone(ZoneInfo(settings.app_timezone)).date()}",
        )
    return created
