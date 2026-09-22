"""الطابور: claim متزامن بلا تكرار، lease مسيَّج (T18)، reaper، وإعادة محاولة محدودة."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Job, Source
from app.jobs import queue
from tests.conftest import WorkspaceFixture


async def _enqueue(
    sm: async_sessionmaker[AsyncSession], n: int, ws: WorkspaceFixture, kind: str = "noop"
) -> None:
    async with sm() as db, db.begin():
        for i in range(n):
            await queue.enqueue(db, kind=kind, workspace_id=ws.id, payload={"i": i})


async def test_concurrent_claims_never_share_a_job(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    await _enqueue(sm, 6, ws)

    async def claim(worker: str) -> queue.Lease | None:
        async with sm() as db:
            return await queue.claim(db, worker, kinds=["noop"])

    leases = await asyncio.gather(*(claim(f"w{i}") for i in range(10)))
    claimed = [lease.job_id for lease in leases if lease is not None]
    assert len(claimed) == 6 and len(set(claimed)) == 6


async def test_stale_worker_cannot_commit_after_lease_lost(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    await _enqueue(sm, 1, ws)
    async with sm() as db:
        old = await queue.claim(db, "old-worker", kinds=["noop"])
    assert old is not None
    # انتهى الحجز واسترده reaper، ثم حجزه عامل جديد (محاولة 2)
    async with sm() as db, db.begin():
        await db.execute(
            update(Job)
            .where(Job.id == old.job_id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    async with sm() as db:
        assert await queue.reap_expired(db) == 1
    async with sm() as db:
        new = await queue.claim(db, "new-worker", kinds=["noop"])
    assert new is not None and new.job_id == old.job_id and new.attempt == old.attempt + 1

    with pytest.raises(queue.LeaseLost):
        async with sm() as db, db.begin():
            await queue.assert_lease(db, old)
    async with sm() as db:
        assert await queue.heartbeat(db, old) is False
    async with sm() as db, db.begin():
        await queue.assert_lease(db, new)
        await queue.complete(db, new, {"ok": True})
    async with sm() as db:
        job = await db.get(Job, new.job_id)
    assert job is not None and job.status == "completed" and job.result == {"ok": True}


async def test_retry_backoff_then_final_failure(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    async with sm() as db, db.begin():
        await queue.enqueue(db, kind="noop", workspace_id=ws.id, payload={}, max_attempts=2)
    async with sm() as db:
        lease = await queue.claim(db, "w", kinds=["noop"])
    assert lease is not None
    async with sm() as db, db.begin():
        assert await queue.fail(db, lease, "boom") == "retry_scheduled"
    async with sm() as db:
        assert await queue.claim(db, "w", kinds=["noop"]) is None  # backoff: ليست مستحقة بعد
        await db.execute(update(Job).values(run_after=datetime.now(UTC)))
        await db.commit()
        lease2 = await queue.claim(db, "w", kinds=["noop"])
    assert lease2 is not None and lease2.attempt == 2
    async with sm() as db, db.begin():
        assert await queue.fail(db, lease2, "boom again") == "failed"


async def test_idempotency_key_returns_existing_job(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    async with sm() as db, db.begin():
        a = await queue.enqueue(
            db,
            kind="noop",
            workspace_id=ws.id,
            payload={},
            idempotency_key=f"{ws.id}:discover:2026-09-22",
        )
        b = await queue.enqueue(
            db,
            kind="noop",
            workspace_id=ws.id,
            payload={},
            idempotency_key=f"{ws.id}:discover:2026-09-22",
        )
    assert a.id == b.id


async def test_failed_sample_job_does_not_leave_source_validating(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    from app.jobs.handlers import HandlerContext
    from app.jobs.worker import Worker
    from tests.conftest import make_settings

    async with sm() as db, db.begin():
        src = Source(
            workspace_id=ws.id,
            name="s",
            kind="manual",
            connector_key="manual",
            access_mode="manual_only",
            status="validating",
        )
        db.add(src)
        await db.flush()
        # مهمة تشير لمصدر في workspace آخر → خطأ دائم
        await queue.enqueue(
            db,
            kind="source_sample",
            workspace_id=None,
            payload={"source_id": str(src.id), "config_version": 1},
        )
    await Worker(HandlerContext(settings=make_settings(), sessionmaker=sm), worker_id="w").drain()
    async with sm() as db:
        job = (await db.execute(select(Job))).scalar_one()
        source = await db.get(Source, src.id)
    assert job.status == "failed" and job.attempts == 1
    assert source is not None and source.status == "failed"
