"""عامل Python منفصل عن الويب.

    uv run python -m app.jobs.worker            # حلقة مستمرة
    uv run python -m app.jobs.worker --once     # ينفذ المستحق ثم يخرج (للاختبار والتشغيل اليدوي)

الإيقاف الرشيق: SIGINT/SIGTERM يوقف سحب مهام جديدة، ويُكمل المهمة الجارية أو يتركها لـreaper.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import socket
import uuid
from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.models import WorkerHeartbeat
from app.db.session import create_engine, loop_factory, make_sessionmaker
from app.jobs import queue
from app.jobs.handlers import FAILURE_HOOKS, HANDLERS, HandlerContext, PermanentJobError

log = logging.getLogger("worker")

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 30
POLL_SECONDS = 5


class Worker:
    def __init__(self, ctx: HandlerContext, worker_id: str | None = None) -> None:
        self.ctx = ctx
        self.sm: async_sessionmaker[AsyncSession] = ctx.sessionmaker
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.stopping = asyncio.Event()

    async def _beat(self, job_id: uuid.UUID | None = None, stopped: bool = False) -> None:
        now = datetime.now(UTC)
        async with self.sm() as db:
            stmt = insert(WorkerHeartbeat).values(
                worker_id=self.worker_id,
                last_seen_at=now,
                current_job_id=job_id,
                stopped_at=now if stopped else None,
            )
            await db.execute(
                stmt.on_conflict_do_update(
                    index_elements=[WorkerHeartbeat.worker_id],
                    set_={
                        "last_seen_at": now,
                        "current_job_id": job_id,
                        "stopped_at": stmt.excluded.stopped_at,
                    },
                )
            )
            await db.commit()

    async def run_one(self) -> bool:
        """يحجز وينفذ مهمة واحدة. يعيد False إن لم توجد مهمة مستحقة."""
        async with self.sm() as db:
            lease = await queue.claim(
                db, self.worker_id, lease_seconds=LEASE_SECONDS, kinds=list(HANDLERS)
            )
        if lease is None:
            return False
        await self._beat(lease.job_id)
        handler = HANDLERS[lease.kind]
        keeper = asyncio.create_task(self._keep_lease(lease))
        try:
            await handler(self.ctx, lease)
            log.info("job %s (%s) completed", lease.job_id, lease.kind)
        except queue.LeaseLost:
            log.warning("job %s: lease lost; result discarded", lease.job_id)
        except Exception as exc:
            retryable = not isinstance(exc, PermanentJobError)
            log.exception("job %s (%s) failed", lease.job_id, lease.kind)
            try:
                error = f"{type(exc).__name__}: {exc}"
                async with self.sm() as db, db.begin():
                    status = await queue.fail(db, lease, error, retryable=retryable)
                    hook = FAILURE_HOOKS.get(lease.kind)
                    if status == "failed" and hook is not None:
                        await hook(db, lease, error)
            except queue.LeaseLost:
                log.warning("job %s: lease lost before recording failure", lease.job_id)
        finally:
            keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keeper
            await self._beat(None)
        return True

    async def _keep_lease(self, lease: queue.Lease) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            async with self.sm() as db:
                if not await queue.heartbeat(db, lease, lease_seconds=LEASE_SECONDS):
                    return

    async def drain(self) -> int:
        count = 0
        async with self.sm() as db:
            await queue.reap_expired(db)
        while not self.stopping.is_set() and await self.run_one():
            count += 1
        return count

    async def run_forever(self) -> None:
        await self._beat()
        log.info("worker %s started", self.worker_id)
        while not self.stopping.is_set():
            try:
                await self.drain()
                await self._beat()
            except Exception:
                log.exception("worker loop error")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), timeout=POLL_SECONDS)
        await self._beat(stopped=True)
        log.info("worker %s stopped", self.worker_id)


async def main(once: bool) -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    engine = create_engine(settings)
    worker = Worker(HandlerContext(settings=settings, sessionmaker=make_sessionmaker(engine)))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(
            NotImplementedError, ValueError
        ):  # Windows لا يدعم add_signal_handler
            loop.add_signal_handler(sig, worker.stopping.set)
    try:
        if once:
            n = await worker.drain()
            log.info("processed %d job(s)", n)
        else:
            await worker.run_forever()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(args.once), loop_factory=loop_factory())
