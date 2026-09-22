"""معالجات المهام. كل معالج: قراءة لقطة → تنفيذ خارج المعاملة → اعتماد مسيَّج بالحجز."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.connectors.netguard import Resolver
from app.connectors.registry import get_connector
from app.db.models import Source
from app.jobs import queue
from app.jobs.queue import Lease
from app.services import sources as source_service


@dataclass
class HandlerContext:
    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    resolver: Resolver | None = None
    transport: httpx.AsyncBaseTransport | None = None


class PermanentJobError(Exception):
    """خطأ لا تفيد إعادة المحاولة فيه."""


Handler = Callable[[HandlerContext, Lease], Awaitable[dict[str, Any] | None]]


async def source_sample(ctx: HandlerContext, lease: Lease) -> dict[str, Any] | None:
    # يُفحص الإعداد الحالي للمصدر حتى لو تغيّر بعد الجدولة؛ النتيجة تُربط بإصدار الإعداد المفحوص.
    source_id = uuid.UUID(lease.payload["source_id"])
    async with ctx.sessionmaker() as db:
        src = await db.get(Source, source_id)
        if src is None or src.workspace_id != lease.workspace_id:
            raise PermanentJobError("المصدر غير موجود")
        config = source_service.source_config(src)
    connector = get_connector(
        config.connector_key, ctx.settings, resolver=ctx.resolver, transport=ctx.transport
    )
    started = time.monotonic()
    result = await connector.sample(config)
    duration_ms = int((time.monotonic() - started) * 1000)
    async with ctx.sessionmaker() as db, db.begin():
        await queue.assert_lease(db, lease)
        src = await db.get(Source, source_id, with_for_update=True)
        if src is None:
            raise PermanentJobError("المصدر حُذف أثناء الفحص")
        check = await source_service.record_check(
            db,
            src,
            result,
            config_version=config.config_version,
            job_id=lease.job_id,
            duration_ms=duration_ms,
        )
        summary = {
            "check_id": str(check.id),
            "status": result.status,
            "requests": result.request_count,
        }
        await queue.complete(db, lease, summary)
    return summary


async def source_sample_failed(db: AsyncSession, lease: Lease, error: str) -> None:
    """فشل نهائي للفحص: لا يبقى المصدر معلقًا في validating."""
    src = await db.get(Source, uuid.UUID(lease.payload["source_id"]), with_for_update=True)
    if src is not None and src.status == "validating":
        src.status = "failed"
        src.status_reason = "تعذر إكمال فحص العينة؛ أعد المحاولة أو راجع الإعداد"
        src.last_error = error[:500]


FailureHook = Callable[[AsyncSession, Lease, str], Awaitable[None]]

HANDLERS: dict[str, Handler] = {
    source_service.SOURCE_SAMPLE_JOB: source_sample,
}
FAILURE_HOOKS: dict[str, FailureHook] = {
    source_service.SOURCE_SAMPLE_JOB: source_sample_failed,
}
