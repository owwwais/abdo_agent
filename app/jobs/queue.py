"""طابور PostgreSQL: claim ذري بـFOR UPDATE SKIP LOCKED وcommit سريع، وlease برقم محاولة
(fencing token) يمنع عاملًا فقد حجزه من اعتماد نتيجة متأخرة."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job


@dataclass(frozen=True)
class Lease:
    job_id: uuid.UUID
    kind: str
    workspace_id: uuid.UUID | None
    payload: dict[str, Any]
    worker_id: str
    attempt: int  # fencing token


class LeaseLost(RuntimeError):
    """العامل فقد الحجز (انتهى أو استرده reaper)؛ يجب إسقاط النتيجة دون أثر."""


def _now() -> datetime:
    return datetime.now(UTC)


async def enqueue(
    db: AsyncSession,
    *,
    kind: str,
    workspace_id: uuid.UUID | None,
    payload: dict[str, Any],
    idempotency_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 3,
) -> Job:
    """يضيف مهمة ضمن معاملة المستدعي. مع idempotency_key: المهمة الموجودة تُعاد بلا تكرار."""
    if idempotency_key:
        existing = (
            await db.execute(select(Job).where(Job.idempotency_key == idempotency_key))
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    job = Job(
        workspace_id=workspace_id,
        kind=kind,
        payload=payload,
        idempotency_key=idempotency_key,
        run_after=run_after or _now(),
        max_attempts=max_attempts,
        status="queued",
    )
    db.add(job)
    await db.flush()
    return job


async def claim(
    db: AsyncSession, worker_id: str, *, lease_seconds: int = 120, kinds: list[str] | None = None
) -> Lease | None:
    """يحجز مهمة مستحقة واحدة ويعتمد الحجز فورًا. لا تبقى معاملة مفتوحة أثناء التنفيذ."""
    kind_filter = "AND kind = ANY(:kinds)" if kinds else ""
    row = (
        await db.execute(
            text(
                f"""
                UPDATE sales.jobs SET
                    status = 'running',
                    attempts = attempts + 1,
                    lease_owner = :worker,
                    lease_until = now() + make_interval(secs => :lease),
                    updated_at = now()
                WHERE id = (
                    SELECT id FROM sales.jobs
                    WHERE status IN ('queued', 'retry_scheduled') AND run_after <= now() {kind_filter}
                    ORDER BY run_after, created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING id, kind, workspace_id, payload, attempts
                """  # noqa: S608  kind_filter ثابت وليس مدخل مستخدم
            ),
            {"worker": worker_id, "lease": lease_seconds, "kinds": kinds or []},
        )
    ).one_or_none()
    await db.commit()
    if row is None:
        return None
    return Lease(row.id, row.kind, row.workspace_id, dict(row.payload), worker_id, row.attempts)


def _held(lease: Lease) -> Any:
    return and_(
        Job.id == lease.job_id,
        Job.status == "running",
        Job.lease_owner == lease.worker_id,
        Job.attempts == lease.attempt,
    )


async def assert_lease(db: AsyncSession, lease: Lease) -> None:
    """داخل معاملة الاعتماد: يقفل صف المهمة ويتحقق أن الحجز ما زال لهذا العامل وهذه المحاولة."""
    held = (
        await db.execute(
            select(Job.id).where(_held(lease), Job.lease_until > _now()).with_for_update()
        )
    ).scalar_one_or_none()
    if held is None:
        raise LeaseLost(str(lease.job_id))


async def heartbeat(db: AsyncSession, lease: Lease, *, lease_seconds: int = 120) -> bool:
    res = await db.execute(
        update(Job)
        .where(_held(lease))
        .values(lease_until=_now() + timedelta(seconds=lease_seconds), updated_at=_now())
    )
    await db.commit()
    return res.rowcount == 1  # type: ignore[attr-defined]


async def complete(db: AsyncSession, lease: Lease, result: dict[str, Any] | None = None) -> None:
    """يُستدعى داخل معاملة الاعتماد نفسها بعد assert_lease."""
    res = await db.execute(
        update(Job)
        .where(_held(lease))
        .values(
            status="completed",
            result=result,
            lease_owner=None,
            lease_until=None,
            finished_at=_now(),
            updated_at=_now(),
        )
    )
    if res.rowcount != 1:  # type: ignore[attr-defined]
        raise LeaseLost(str(lease.job_id))


async def fail(db: AsyncSession, lease: Lease, error: str, *, retryable: bool = True) -> str:
    """يسجل فشلًا: إعادة محاولة مع backoff أسي أو فشل نهائي. يعيد الحالة الجديدة."""
    job = (await db.execute(select(Job).where(_held(lease)).with_for_update())).scalar_one_or_none()
    if job is None:
        raise LeaseLost(str(lease.job_id))
    final = not retryable or job.attempts >= job.max_attempts
    job.status = "failed" if final else "retry_scheduled"
    job.last_error = error[:2000]
    job.lease_owner = None
    job.lease_until = None
    if final:
        job.finished_at = _now()
    else:
        job.run_after = _now() + timedelta(seconds=min(600, 5 * 2 ** (job.attempts - 1)))
    await db.flush()
    return job.status


async def reap_expired(db: AsyncSession) -> int:
    """يسترد حجوزات منتهية: تعاد للجدولة ضمن حد المحاولات، وإلا تفشل. لا يمس الإرسال الملتبس
    (يُعالج في مسار مصالحة منفصل في M4)."""
    now = _now()
    expired = list(
        (
            await db.execute(
                select(Job)
                .where(Job.status == "running", Job.lease_until < now)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    for job in expired:
        final = job.attempts >= job.max_attempts
        job.status = "failed" if final else "retry_scheduled"
        job.last_error = "lease_expired"
        job.lease_owner = None
        job.lease_until = None
        job.run_after = now
        if final:
            job.finished_at = now
    await db.commit()
    return len(expired)


def claimable_filter() -> Any:
    return and_(
        or_(Job.status == "queued", Job.status == "retry_scheduled"), Job.run_after <= _now()
    )
