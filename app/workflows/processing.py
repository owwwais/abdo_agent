"""نافذة معالجة الفرص (10:00 و12:00 و14:00 افتراضيًا) وقفل المبيعات لكل workspace.

- قفل advisory على مستوى الجلسة يضمن فرصة مبيعات واحدة قيد التنفيذ حتى لو شُغّل عامل ثانٍ بالخطأ.
- حجز خانة الحصة اليومية ذري قبل المعالجة؛ الفرصة غير المؤهلة تعيد الخانة ويُجرب المرشح التالي (حد 3).
- الانتظار للموافقة لا يحجز العامل: الخيط محفوظ في PostgreSQL والعامل يتحرر.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.models_sales import Opportunity
from app.services import integrations as integ
from app.services import runs
from app.workflows.common import Deps, local_today, workspace_tz
from app.workflows.opportunity import next_candidate, run_opportunity

MAX_ATTEMPTS_PER_WINDOW = 3


class LockBusy(Exception):
    """قفل المبيعات مشغول بمعالجة أخرى؛ تُعاد جدولة المهمة لاحقًا دون اعتبارها فشلًا."""


def _lock_key(workspace_id: uuid.UUID) -> int:
    return int.from_bytes(
        uuid.uuid5(uuid.NAMESPACE_URL, f"sales-lock:{workspace_id}").bytes[:8], "big", signed=True
    )


@asynccontextmanager
async def sales_lock(deps: Deps, workspace_id: uuid.UUID) -> AsyncIterator[None]:
    engine = deps.sm.kw["bind"]
    conn: AsyncConnection
    async with engine.connect() as conn:
        got = (
            await conn.execute(select(func.pg_try_advisory_lock(_lock_key(workspace_id))))
        ).scalar_one()
        await conn.commit()
        if not got:
            raise LockBusy("معالجة مبيعات أخرى جارية لهذه الشركة")
        try:
            yield
        finally:
            await conn.execute(select(func.pg_advisory_unlock(_lock_key(workspace_id))))
            await conn.commit()


async def run_window(
    deps: Deps,
    workspace_id: uuid.UUID,
    *,
    job_id: uuid.UUID | None = None,
    opportunity_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    async with deps.sm() as db:
        ops = await integ.get_config(
            db, deps.settings, workspace_id, "operations", integ.OperationsConfig
        )
    if ops.pause_all_processing:
        return {"status": "canceled", "reason": "المعالجة موقوفة"}
    tz = await workspace_tz(deps.sm, workspace_id, deps.settings.app_timezone)
    today = local_today(tz)
    results: list[dict[str, Any]] = []
    tried: list[uuid.UUID] = []
    async with sales_lock(deps, workspace_id):
        for _ in range(1 if opportunity_id else MAX_ATTEMPTS_PER_WINDOW):
            async with deps.sm() as db, db.begin():
                if not await runs.reserve_slot(db, workspace_id, today, ops.max_qualified_per_day):
                    return {
                        "status": "quota_full",
                        "reason": "اكتملت حصة الفرص المؤهلة اليوم",
                        "results": results,
                    }
            target = opportunity_id or await next_candidate(deps, workspace_id, tried)
            if target is None:
                async with deps.sm() as db, db.begin():
                    await runs.release_slot(db, workspace_id, today)
                return {
                    "status": "no_candidates",
                    "reason": "لا مرشحين مكتشفين بانتظار المعالجة",
                    "results": results,
                }
            tried.append(target)
            result = await run_opportunity(deps, workspace_id, target, job_id=job_id)
            results.append(
                {
                    "opportunity_id": str(target),
                    **{k: result.get(k) for k in ("status", "reason", "draft_id")},
                }
            )
            async with deps.sm() as db, db.begin():
                if result["status"] == "waiting_approval":
                    await runs.use_slot(db, workspace_id, today)
                    return {"status": "draft_ready", "results": results}
                await runs.release_slot(db, workspace_id, today)
                # مرشح لم يكتمل لسبب مؤقت لا يُعاد اختياره في النافذة نفسها.
                await db.execute(
                    update(Opportunity)
                    .where(Opportunity.id == target, Opportunity.status == "discovered")
                    .values(next_action_at=datetime.now(UTC) + timedelta(days=1))
                )
            if result.get("outcome") in ("blocked_budget",):
                return {
                    "status": "blocked_budget",
                    "reason": result.get("reason"),
                    "results": results,
                }
    return {"status": "completed", "results": results}
