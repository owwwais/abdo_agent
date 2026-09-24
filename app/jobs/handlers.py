"""معالجات المهام. كل معالج: قراءة لقطة → تنفيذ خارج المعاملة → اعتماد مسيَّج بالحجز."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from app.services.channels import ChannelNotReady, maps_context, search_context


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
    search_client = None
    if config.connector_key in ("web_search", "fake_search"):
        async with ctx.sessionmaker() as db:
            try:
                search_client = (await search_context(db, ctx.settings, config.workspace_id)).client
            except ChannelNotReady:
                search_client = None
    places_client = None
    if config.connector_key == "google_maps":
        async with ctx.sessionmaker() as db:
            try:
                places_client = (await maps_context(db, ctx.settings, config.workspace_id)).client
            except ChannelNotReady:
                places_client = None
    connector = get_connector(
        config.connector_key,
        ctx.settings,
        resolver=ctx.resolver,
        transport=ctx.transport,
        search=search_client,
        places=places_client,
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


def _deps(ctx: HandlerContext) -> Any:
    from app.workflows.common import Deps

    return Deps(
        settings=ctx.settings, sm=ctx.sessionmaker, resolver=ctx.resolver, transport=ctx.transport
    )


async def _finish(ctx: HandlerContext, lease: Lease, result: dict[str, Any]) -> dict[str, Any]:
    async with ctx.sessionmaker() as db, db.begin():
        await queue.assert_lease(db, lease)
        simple = {
            k: v for k, v in result.items() if isinstance(v, (str, int, float, bool, type(None)))
        }
        await queue.complete(db, lease, simple)
    return result


def _ws(lease: Lease) -> uuid.UUID:
    if lease.workspace_id is None:
        raise PermanentJobError("المهمة بلا workspace")
    return lease.workspace_id


async def _reschedule(ctx: HandlerContext, lease: Lease, minutes: int, note: str) -> dict[str, Any]:
    async with ctx.sessionmaker() as db, db.begin():
        await queue.reschedule(db, lease, datetime.now(UTC) + timedelta(minutes=minutes), note)
    return {"status": "rescheduled"}


async def discover_daily(ctx: HandlerContext, lease: Lease) -> dict[str, Any] | None:
    from app.workflows.discovery import run_discovery

    return await _finish(
        ctx, lease, await run_discovery(_deps(ctx), _ws(lease), job_id=lease.job_id)
    )


async def process_window(ctx: HandlerContext, lease: Lease) -> dict[str, Any] | None:
    from app.workflows.processing import LockBusy, run_window

    opp = lease.payload.get("opportunity_id")
    try:
        result = await run_window(
            _deps(ctx),
            _ws(lease),
            job_id=lease.job_id,
            opportunity_id=uuid.UUID(opp) if opp else None,
        )
    except LockBusy as exc:
        return await _reschedule(ctx, lease, 2, str(exc))
    return await _finish(
        ctx, lease, {"status": result["status"], "reason": result.get("reason", "")}
    )


async def resume_opportunity(ctx: HandlerContext, lease: Lease) -> dict[str, Any] | None:
    from app.workflows.opportunity import resume_opportunity as resume

    result = await resume(_deps(ctx), lease.payload["thread_id"], lease.payload.get("decision"))
    return await _finish(ctx, lease, result)


async def send_outbound(ctx: HandlerContext, lease: Lease) -> dict[str, Any] | None:
    from app.services.outbound import send_outbound as send

    return await send(_deps(ctx), lease)


async def notify_draft(ctx: HandlerContext, lease: Lease) -> dict[str, Any] | None:
    from app.services.telegram_bot import send_draft_card

    result = await send_draft_card(_deps(ctx), _ws(lease), uuid.UUID(lease.payload["draft_id"]))
    return await _finish(ctx, lease, result)


async def mail_sync(ctx: HandlerContext, lease: Lease) -> dict[str, Any] | None:
    from app.services.inbound import sync_mailbox

    return await _finish(ctx, lease, await sync_mailbox(_deps(ctx), _ws(lease)))


async def process_reply(ctx: HandlerContext, lease: Lease) -> dict[str, Any] | None:
    from app.services.inbound import draft_reply
    from app.workflows.processing import LockBusy, sales_lock

    try:
        async with sales_lock(_deps(ctx), _ws(lease)):
            result = await draft_reply(
                _deps(ctx), _ws(lease), uuid.UUID(lease.payload["message_id"])
            )
    except LockBusy as exc:
        return await _reschedule(ctx, lease, 2, str(exc))
    return await _finish(ctx, lease, result)


async def digest(ctx: HandlerContext, lease: Lease) -> dict[str, Any] | None:
    from app.services.digest import run_digest

    return await _finish(ctx, lease, await run_digest(_deps(ctx), _ws(lease)))


async def cleanup(ctx: HandlerContext, lease: Lease) -> dict[str, Any] | None:
    from app.services.cleanup import run_cleanup

    return await _finish(ctx, lease, await run_cleanup(_deps(ctx)))


async def noop(ctx: HandlerContext, lease: Lease) -> dict[str, Any] | None:
    return await _finish(ctx, lease, {"status": "noop"})


FailureHook = Callable[[AsyncSession, Lease, str], Awaitable[None]]

HANDLERS: dict[str, Handler] = {
    source_service.SOURCE_SAMPLE_JOB: source_sample,
    "discover_daily": discover_daily,
    "process_window": process_window,
    "resume_opportunity": resume_opportunity,
    "send_outbound": send_outbound,
    "notify_draft": notify_draft,
    "mail_sync": mail_sync,
    "process_reply": process_reply,
    "digest": digest,
    "cleanup": cleanup,
    "noop": noop,
}
FAILURE_HOOKS: dict[str, FailureHook] = {
    source_service.SOURCE_SAMPLE_JOB: source_sample_failed,
}
