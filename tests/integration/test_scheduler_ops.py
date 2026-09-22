"""M5: المجدول والملخص والتنظيف (F-012، F-013، T20، T22)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Job
from app.db.models_ops import Digest, Run
from app.db.models_sales import Approval, Evidence
from app.jobs.scheduler import schedule_due
from app.services import integrations as integ
from app.services.channels import TEST_HOOKS
from app.services.cleanup import run_cleanup
from app.services.digest import run_digest
from app.services.secrets import set_secret
from app.workflows.common import Deps
from tests.conftest import WorkspaceFixture, make_settings
from tests.sales_fixtures import approve, seed_draft

RIYADH = ZoneInfo("Asia/Riyadh")


def at(hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 9, 22, hh, mm, tzinfo=RIYADH).astimezone(UTC)


async def live(sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture, **ops: Any) -> None:
    async with sm() as db, db.begin():
        await integ.save_config(
            db,
            ws.id,
            "operations",
            integ.OperationsConfig(
                operating_mode="live",
                discovery_time="08:00",
                process_slots=["10:00"],
                digest_time="18:00",
                grace_hours=2,
                **ops,
            ),
            version=0,
            actor_id="t",
        )


async def kinds(sm: async_sessionmaker[AsyncSession]) -> list[str]:
    async with sm() as db:
        return sorted(j.kind for j in (await db.execute(select(Job))).scalars())


async def test_demo_mode_schedules_nothing_but_cleanup(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    await schedule_due(sm, make_settings(), at(8, 30))
    assert await kinds(sm) == ["cleanup"]


async def test_live_schedule_is_idempotent_within_window(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    await live(sm, ws)
    for minute in (5, 6, 30):
        await schedule_due(sm, make_settings(), at(8, minute))
    assert await kinds(sm) == ["cleanup", "discover_daily"]
    await schedule_due(sm, make_settings(), at(10, 1))
    assert (await kinds(sm)).count("process_window") == 1


async def test_missed_window_is_not_compensated(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    """T20: العامل كان متوقفًا طوال نافذة الاكتشاف → تشغيل مُلغى واحد، ولا دورة متأخرة."""
    await live(sm, ws)
    await schedule_due(sm, make_settings(), at(11, 0))
    await schedule_due(sm, make_settings(), at(11, 5))
    assert "discover_daily" not in await kinds(sm)
    async with sm() as db:
        runs = list((await db.execute(select(Run).where(Run.kind == "discover"))).scalars())
    assert len(runs) == 1 and runs[0].status == "canceled"


async def test_paused_discovery_is_not_scheduled(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    await live(sm, ws, pause_discovery=True)
    await schedule_due(sm, make_settings(), at(8, 10))
    assert "discover_daily" not in await kinds(sm)


async def test_digest_is_sent_to_telegram_once(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 1, "chat": {"id": -100123}}}
        )

    TEST_HOOKS["telegram_transport"] = httpx.MockTransport(handler)
    try:
        async with sm() as db, db.begin():
            await set_secret(db, make_settings(), ws.id, "telegram_bot_token", "1:tok", "t")
            await integ.save_config(
                db,
                ws.id,
                "telegram",
                integ.TelegramConfig(enabled=True, chat_id="-100123"),
                version=0,
                actor_id="t",
            )
        d = Deps(settings=make_settings(), sm=sm)
        first = await run_digest(d, ws.id)
        second = await run_digest(d, ws.id)
    finally:
        TEST_HOOKS["telegram_transport"] = None
    assert first["status"] == "sent" and second["status"] == "already_sent"
    assert len(sent) == 1 and "ملخص" in sent[0]["text"]
    async with sm() as db:
        assert (await db.execute(select(func.count()).select_from(Digest))).scalar_one() == 1


async def test_cleanup_expires_approvals_and_deletes_old_evidence(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    """T22: الأدلة المنتهية تُحذف، والاعتمادات المنتهية لا تُستعمل."""
    s = await seed_draft(sm, ws)
    await approve(sm, ws, s.draft)
    now = datetime.now(UTC)
    async with sm() as db, db.begin():
        db.add(
            Evidence(
                workspace_id=ws.id,
                company_id=s.company,
                fact_type="booking",
                claim="قديم",
                expires_at=now - timedelta(days=1),
            )
        )
        db.add(
            Evidence(
                workspace_id=ws.id,
                company_id=s.company,
                fact_type="booking",
                claim="ساري",
                expires_at=now + timedelta(days=30),
            )
        )
        approval = (await db.execute(select(Approval))).scalar_one()
        approval.expires_at = now - timedelta(minutes=1)
    out = await run_cleanup(Deps(settings=make_settings(), sm=sm))
    assert out["evidence_deleted"] == 1 and out["approvals_expired"] == 1
    async with sm() as db:
        claims = [e.claim for e in (await db.execute(select(Evidence))).scalars()]
        approval = (await db.execute(select(Approval))).scalar_one()
    assert claims == ["ساري"] and approval.status == "expired"


async def test_worker_drains_the_approval_to_reply_cycle(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    """من الاعتماد إلى الإرسال ثم المزامنة والرد المقترح، كله عبر العامل ومعالجات المهام الحقيقية."""
    from app.connectors.mail import FakeMailbox
    from app.db.models_sales import Draft, Message, OutboundCommand
    from app.jobs import queue
    from app.jobs.handlers import HandlerContext
    from app.jobs.worker import Worker
    from tests.sales_fixtures import inbound

    FakeMailbox.reset()
    try:
        s = await seed_draft(sm, ws)
        await approve(sm, ws, s.draft)
        worker = Worker(
            HandlerContext(settings=make_settings(), sessionmaker=sm), worker_id="w-e2e"
        )
        assert await worker.drain() >= 1
        assert len(FakeMailbox.outbox) == 1
        FakeMailbox.inbox.append(
            inbound(
                1,
                body="نحن مهتمون، ما الخطوة التالية؟",
                in_reply_to=str(FakeMailbox.outbox[0]["Message-ID"]),
            )
        )
        async with sm() as db, db.begin():
            await queue.enqueue(
                db, kind="mail_sync", workspace_id=ws.id, payload={}, idempotency_key="sync-1"
            )
        await worker.drain()  # mail_sync ثم process_reply
        async with sm() as db:
            cmd = (await db.execute(select(OutboundCommand))).scalar_one()
            inbound_count = (
                await db.execute(
                    select(func.count()).select_from(Message).where(Message.direction == "inbound")
                )
            ).scalar_one()
            reply = (await db.execute(select(Draft).where(Draft.kind == "reply"))).scalar_one()
            failed = (
                await db.execute(
                    select(func.count()).select_from(Job).where(Job.status == "failed")
                )
            ).scalar_one()
        assert cmd.status == "sent" and inbound_count == 1
        assert reply.status == "pending_review" and failed == 0
        assert len(FakeMailbox.outbox) == 1  # الرد المقترح لا يُرسل دون اعتماد
    finally:
        FakeMailbox.reset()
