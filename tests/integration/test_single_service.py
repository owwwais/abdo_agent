"""خدمة واحدة (خطة Render المجانية): العامل المدمج يعمل داخل عملية الويب ويتوقف معها."""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.models import Job, WorkerHeartbeat
from app.jobs import queue
from app.main import create_app
from tests.conftest import ClientFactory, WorkspaceFixture, make_settings


async def job_status(sm: async_sessionmaker[AsyncSession], key: str) -> str:
    async with sm() as db:
        return (await db.execute(select(Job.status).where(Job.idempotency_key == key))).scalar_one()


async def test_embedded_worker_runs_jobs_and_stops_with_app(
    engine: AsyncEngine, sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    app = create_app(make_settings(run_worker_in_web=True), engine=engine)
    async with app.router.lifespan_context(app):
        async with sm() as db, db.begin():
            await queue.enqueue(
                db, kind="noop", workspace_id=ws.id, payload={}, idempotency_key="embedded-1"
            )
        status = ""
        for _ in range(200):
            status = await job_status(sm, "embedded-1")
            if status == "completed":
                break
            await asyncio.sleep(0.1)
        assert status == "completed"
        async with sm() as db:
            alive = (
                (
                    await db.execute(
                        select(WorkerHeartbeat).where(WorkerHeartbeat.stopped_at.is_(None))
                    )
                )
                .scalars()
                .all()
            )
        assert alive, "العامل المدمج لم يسجل نبضة"
    async with sm() as db:
        beats = list((await db.execute(select(WorkerHeartbeat))).scalars())
    assert beats and all(b.stopped_at is not None for b in beats)  # توقف مع إغلاق التطبيق


async def test_worker_not_started_by_default(
    engine: AsyncEngine, sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    app = create_app(make_settings(), engine=engine)
    async with app.router.lifespan_context(app):
        async with sm() as db, db.begin():
            await queue.enqueue(
                db, kind="noop", workspace_id=ws.id, payload={}, idempotency_key="embedded-2"
            )
        await asyncio.sleep(1)
        assert await job_status(sm, "embedded-2") == "queued"


async def test_health_endpoints_accept_head_for_uptime_monitors(client_for: ClientFactory) -> None:
    client = await client_for()
    for path in ("/health/live", "/health/ready"):
        assert (await client.head(path)).status_code == 200
        assert (await client.get(path)).status_code == 200
