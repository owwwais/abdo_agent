"""سجل التشغيلات وخطواتها (منقح: لا أسرار ولا نصوص خام طويلة ولا سلسلة تفكير)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models_ops import DailyQuota, Run, RunEvent


async def start_run(
    sm: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    kind: str,
    *,
    job_id: uuid.UUID | None = None,
    opportunity_id: uuid.UUID | None = None,
    snapshot: dict[str, Any] | None = None,
) -> uuid.UUID:
    async with sm() as db, db.begin():
        run = Run(
            workspace_id=workspace_id,
            kind=kind,
            status="running",
            job_id=job_id,
            opportunity_id=opportunity_id,
            config_snapshot=snapshot or {},
        )
        db.add(run)
        await db.flush()
        return run.id


async def event(
    sm: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    step: str,
    summary: str,
    *,
    event_type: str = "info",
    data: dict[str, Any] | None = None,
) -> None:
    async with sm() as db, db.begin():
        db.add(
            RunEvent(
                workspace_id=workspace_id,
                run_id=run_id,
                step=step,
                event_type=event_type,
                sanitized_summary=summary[:500],
                data=data or {},
            )
        )


async def finish_run(
    sm: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    status: str,
    *,
    summary: dict[str, Any] | None = None,
    error_code: str | None = None,
    thread_id: str | None = None,
) -> None:
    values: dict[str, Any] = {"status": status, "error_code": error_code}
    if status not in ("waiting_approval", "running"):
        values["ended_at"] = datetime.now(UTC)
    if summary is not None:
        values["summary"] = summary
    if thread_id is not None:
        values["graph_thread_id"] = thread_id
    async with sm() as db, db.begin():
        await db.execute(update(Run).where(Run.id == run_id).values(**values))


async def _ensure_quota_row(db: AsyncSession, workspace_id: uuid.UUID, local_date: date) -> None:
    await db.execute(
        insert(DailyQuota)
        .values(workspace_id=workspace_id, local_date=local_date)
        .on_conflict_do_nothing()
    )


async def reserve_slot(
    db: AsyncSession, workspace_id: uuid.UUID, local_date: date, limit: int
) -> bool:
    """حجز ذري لخانة فرصة مؤهلة اليوم؛ لا يتجاوز الحد مهما تزامنت الطلبات."""
    await _ensure_quota_row(db, workspace_id, local_date)
    res = await db.execute(
        update(DailyQuota)
        .where(
            DailyQuota.workspace_id == workspace_id,
            DailyQuota.local_date == local_date,
            DailyQuota.qualified_slots_reserved < limit,
        )
        .values(qualified_slots_reserved=DailyQuota.qualified_slots_reserved + 1)
        .returning(DailyQuota.qualified_slots_reserved)
    )
    return res.scalar_one_or_none() is not None


async def release_slot(db: AsyncSession, workspace_id: uuid.UUID, local_date: date) -> None:
    await db.execute(
        update(DailyQuota)
        .where(
            DailyQuota.workspace_id == workspace_id,
            DailyQuota.local_date == local_date,
            DailyQuota.qualified_slots_reserved > DailyQuota.qualified_slots_used,
        )
        .values(qualified_slots_reserved=DailyQuota.qualified_slots_reserved - 1)
    )


async def use_slot(db: AsyncSession, workspace_id: uuid.UUID, local_date: date) -> None:
    await db.execute(
        update(DailyQuota)
        .where(DailyQuota.workspace_id == workspace_id, DailyQuota.local_date == local_date)
        .values(qualified_slots_used=DailyQuota.qualified_slots_used + 1)
    )


async def count_discovery(db: AsyncSession, workspace_id: uuid.UUID, local_date: date) -> None:
    await _ensure_quota_row(db, workspace_id, local_date)
    await db.execute(
        update(DailyQuota)
        .where(DailyQuota.workspace_id == workspace_id, DailyQuota.local_date == local_date)
        .values(discovery_count=DailyQuota.discovery_count + 1)
    )
