"""الميزانية: حجز ذري للحد الأعلى قبل أي طلب مدفوع، ثم تسوية بالاستهلاك الفعلي، وسجل استهلاك.

الحجز يقفل صف الـworkspace فيتسلسل تحقق الميزانية بين العمال؛ لا «فحص ثم خصم» منفصلان (SPEC §9.3).
لا ضمان لسقف فاتورة مطلق دون بيانات فوترة فورية من المزود؛ التسوية تعتمد التوكنات المسجلة وسعر الإعداد.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import AppError
from app.db.models import Workspace
from app.db.models_ops import BudgetReservation, UsageLedger
from app.services.integrations import OperationsConfig

RESERVATION_TTL = timedelta(minutes=30)
ZERO = Decimal("0")
TEST_ALLOWANCE = Decimal("0.05")


class BudgetBlocked(AppError):
    status_code = 409
    code = "budget_blocked"
    message = "الميزانية لا تسمح بهذا الطلب"


def local_day_bounds(tz_name: str, now: datetime | None = None) -> tuple[datetime, datetime, date]:
    tz = ZoneInfo(tz_name)
    now = (now or datetime.now(UTC)).astimezone(tz)
    start = datetime.combine(now.date(), time.min, tzinfo=tz)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC), now.date()


def local_month_start(tz_name: str, now: datetime | None = None) -> datetime:
    tz = ZoneInfo(tz_name)
    now = (now or datetime.now(UTC)).astimezone(tz)
    return datetime(now.year, now.month, 1, tzinfo=tz).astimezone(UTC)


async def _spent_since(
    db: AsyncSession, workspace_id: uuid.UUID, since: datetime, category: str | None = None
) -> Decimal:
    q = select(
        func.coalesce(
            func.sum(func.coalesce(UsageLedger.actual_cost, UsageLedger.estimated_cost)), 0
        )
    ).where(UsageLedger.workspace_id == workspace_id, UsageLedger.occurred_at >= since)
    if category:
        q = q.where(UsageLedger.category == category)
    return Decimal((await db.execute(q)).scalar_one())


async def _reserved_since(
    db: AsyncSession, workspace_id: uuid.UUID, since: datetime, category: str | None = None
) -> Decimal:
    q = select(func.coalesce(func.sum(BudgetReservation.amount), 0)).where(
        BudgetReservation.workspace_id == workspace_id,
        BudgetReservation.status == "active",
        BudgetReservation.expires_at > datetime.now(UTC),
        BudgetReservation.created_at >= since,
    )
    if category:
        q = q.where(BudgetReservation.scope_key == category)
    return Decimal((await db.execute(q)).scalar_one())


async def usage_summary(
    db: AsyncSession, workspace_id: uuid.UUID, tz_name: str
) -> dict[str, Decimal]:
    day_start, _, _ = local_day_bounds(tz_name)
    month_start = local_month_start(tz_name)
    return {
        "day_spent": await _spent_since(db, workspace_id, day_start),
        "day_reserved": await _reserved_since(db, workspace_id, day_start),
        "month_spent": await _spent_since(db, workspace_id, month_start),
    }


async def reserve(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    ops: OperationsConfig,
    *,
    amount: Decimal,
    category: str,
    run_id: uuid.UUID | None,
    tz_name: str,
) -> uuid.UUID | None:
    """يحجز المبلغ ضمن معاملة المستدعي أو يرفع BudgetBlocked. المبلغ الصفري لا يحتاج حجزًا."""
    if amount <= ZERO:
        return None
    if (
        category == "test"
        and amount <= TEST_ALLOWANCE
        and (ops.daily_budget is None or ops.monthly_budget is None)
    ):
        # اختبار اتصال صغير جدًا مسموح قبل ضبط الميزانية؛ يُسجل في سجل الاستهلاك كالعادة.
        return None
    if ops.daily_budget is None or ops.monthly_budget is None:
        raise BudgetBlocked(
            "حدد الميزانية اليومية والشهرية في الإعدادات قبل أي طلب مدفوع", code="budget_not_set"
        )
    # قفل صف الـworkspace يسلسل كل الحجوزات المتزامنة لنفس الشركة.
    await db.execute(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update())
    day_start, _, _ = local_day_bounds(tz_name)
    month_start = local_month_start(tz_name)
    day_used = await _spent_since(db, workspace_id, day_start) + await _reserved_since(
        db, workspace_id, day_start
    )
    month_used = await _spent_since(db, workspace_id, month_start) + await _reserved_since(
        db, workspace_id, month_start
    )
    if day_used + amount > ops.daily_budget:
        raise BudgetBlocked(
            f"بلغت الميزانية اليومية ({ops.daily_budget} {ops.currency})",
            code="daily_budget_exceeded",
        )
    if month_used + amount > ops.monthly_budget:
        raise BudgetBlocked(
            f"بلغت الميزانية الشهرية ({ops.monthly_budget} {ops.currency})",
            code="monthly_budget_exceeded",
        )
    cap = {
        "discovery": ops.discovery_daily_cap,
        "new_opportunity": ops.new_opportunity_daily_cap,
        "followup": ops.followup_daily_cap,
    }.get(category)
    if cap is not None:
        cat_used = await _spent_since(
            db, workspace_id, day_start, category
        ) + await _reserved_since(db, workspace_id, day_start, category)
        if cat_used + amount > cap:
            raise BudgetBlocked(f"بلغ سقف فئة {category} اليومي", code="category_budget_exceeded")
    res = BudgetReservation(
        workspace_id=workspace_id,
        run_id=run_id,
        scope_key=category,
        amount=amount,
        currency=ops.currency,
        status="active",
        expires_at=datetime.now(UTC) + RESERVATION_TTL,
    )
    db.add(res)
    await db.flush()
    return res.id


async def settle(db: AsyncSession, reservation_id: uuid.UUID | None, actual: Decimal) -> None:
    if reservation_id is None:
        return
    await db.execute(
        update(BudgetReservation)
        .where(BudgetReservation.id == reservation_id)
        .values(status="settled", settled_amount=actual)
    )


async def release(db: AsyncSession, reservation_id: uuid.UUID | None) -> None:
    if reservation_id is None:
        return
    await db.execute(
        update(BudgetReservation)
        .where(BudgetReservation.id == reservation_id, BudgetReservation.status == "active")
        .values(status="released", settled_amount=ZERO)
    )


async def expire_stale(db: AsyncSession) -> int:
    res = await db.execute(
        update(BudgetReservation)
        .where(
            BudgetReservation.status == "active", BudgetReservation.expires_at < datetime.now(UTC)
        )
        .values(status="released")
    )
    return int(res.rowcount or 0)  # type: ignore[attr-defined]


def record_usage(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID | None,
    opportunity_id: uuid.UUID | None,
    category: str,
    kind: str,
    provider: str,
    model: str,
    role: str,
    input_tokens: int,
    output_tokens: int,
    requests: int,
    estimated_cost: Decimal,
    currency: str,
    pricing_version: str,
) -> None:
    db.add(
        UsageLedger(
            workspace_id=workspace_id,
            run_id=run_id,
            opportunity_id=opportunity_id,
            category=category,
            kind=kind,
            provider=provider,
            model=model,
            role=role,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            request_count=requests,
            estimated_cost=estimated_cost,
            actual_cost=None,
            currency=currency,
            pricing_version=pricing_version,
        )
    )
