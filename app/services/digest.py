"""الملخص المسائي (SPEC §11.4): المصادر، المرشحون، الفرص وأسبابها، الموافقات، الرسائل والردود، التكلفة،
العوائق، والخطوة التالية. نقص البيانات يظهر صراحة. سجل واحد لكل يوم؛ لا يُرسل مرتين."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Job, Source
from app.db.models_ops import Digest, Run, UsageLedger
from app.db.models_sales import Approval, Draft, Message, Opportunity, OutboundCommand
from app.services.telegram_bot import send_text
from app.workflows.common import Deps, local_today, workspace_tz


async def build_digest(
    deps: Deps, workspace_id: uuid.UUID, day: date, tz_name: str
) -> dict[str, Any]:
    tz = ZoneInfo(tz_name)
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)
    end = start + timedelta(days=1)
    async with deps.sm() as db:
        discover = list(
            (
                await db.execute(
                    select(Run).where(
                        Run.workspace_id == workspace_id,
                        Run.kind == "discover",
                        Run.started_at >= start,
                        Run.started_at < end,
                    )
                )
            ).scalars()
        )
        opp_status = dict(
            (
                await db.execute(
                    select(Opportunity.status, func.count())
                    .where(
                        Opportunity.workspace_id == workspace_id,
                        Opportunity.updated_at >= start,
                        Opportunity.updated_at < end,
                    )
                    .group_by(Opportunity.status)
                )
            )
            .tuples()
            .all()
        )
        decisions = dict(
            (
                await db.execute(
                    select(Approval.decision, func.count())
                    .where(
                        Approval.workspace_id == workspace_id,
                        Approval.decided_at >= start,
                        Approval.decided_at < end,
                    )
                    .group_by(Approval.decision)
                )
            )
            .tuples()
            .all()
        )
        sent = (
            await db.execute(
                select(func.count())
                .select_from(OutboundCommand)
                .where(
                    OutboundCommand.workspace_id == workspace_id,
                    OutboundCommand.status == "sent",
                    OutboundCommand.sent_at >= start,
                    OutboundCommand.sent_at < end,
                )
            )
        ).scalar_one()
        replies = dict(
            (
                await db.execute(
                    select(Message.classification, func.count())
                    .where(
                        Message.workspace_id == workspace_id,
                        Message.direction == "inbound",
                        Message.created_at >= start,
                        Message.created_at < end,
                    )
                    .group_by(Message.classification)
                )
            )
            .tuples()
            .all()
        )
        cost = dict(
            (
                await db.execute(
                    select(
                        UsageLedger.kind,
                        func.coalesce(
                            func.sum(
                                func.coalesce(UsageLedger.actual_cost, UsageLedger.estimated_cost)
                            ),
                            0,
                        ),
                    )
                    .where(
                        UsageLedger.workspace_id == workspace_id,
                        UsageLedger.occurred_at >= start,
                        UsageLedger.occurred_at < end,
                    )
                    .group_by(UsageLedger.kind)
                )
            )
            .tuples()
            .all()
        )
        pending = (
            await db.execute(
                select(func.count())
                .select_from(Draft)
                .where(Draft.workspace_id == workspace_id, Draft.status == "pending_review")
            )
        ).scalar_one()
        unknown = (
            await db.execute(
                select(func.count())
                .select_from(OutboundCommand)
                .where(
                    OutboundCommand.workspace_id == workspace_id,
                    OutboundCommand.status == "unknown_delivery",
                )
            )
        ).scalar_one()
        failed_jobs = (
            await db.execute(
                select(func.count())
                .select_from(Job)
                .where(
                    Job.workspace_id == workspace_id,
                    Job.status == "failed",
                    Job.updated_at >= start,
                )
            )
        ).scalar_one()
        bad_sources = list(
            (
                await db.execute(
                    select(Source.name).where(
                        Source.workspace_id == workspace_id,
                        Source.status.in_(("failed", "restricted", "needs_setup")),
                    )
                )
            ).scalars()
        )
        blocked_runs = list(
            (
                await db.execute(
                    select(Run.kind, Run.status, Run.summary).where(
                        Run.workspace_id == workspace_id,
                        Run.started_at >= start,
                        Run.started_at < end,
                        Run.status.in_(
                            ("blocked_budget", "blocked_policy", "failed", "skipped_configuration")
                        ),
                    )
                )
            ).tuples()
        )
    blockers = []
    if unknown:
        blockers.append(f"{unknown} رسالة بتسليم غير معروف تحتاج مصالحة")
    if failed_jobs:
        blockers.append(f"{failed_jobs} مهمة فشلت اليوم")
    if bad_sources:
        blockers.append("مصادر متعطلة: " + "، ".join(bad_sources[:5]))
    for kind, status, summary in blocked_runs[:5]:
        blockers.append(f"{kind}: {status} — {str((summary or {}).get('reason', ''))[:120]}")
    stats = discover[0].summary.get("stats", {}) if discover else {}
    content = {
        "date": day.isoformat(),
        "discovery": {
            "ran": bool(discover),
            "status": discover[0].status if discover else None,
            "selection": discover[0].summary.get("selection_reason") if discover else None,
            "queries": discover[0].summary.get("queries") if discover else [],
            "stats": stats,
        },
        "opportunities": opp_status,
        "decisions": decisions,
        "sent": sent,
        "replies": replies,
        "cost": {k: str(Decimal(v)) for k, v in cost.items()},
        "pending_approvals": pending,
        "blockers": blockers,
    }
    lines = [f"📊 ملخص {day.isoformat()}"]
    if discover:
        lines.append(
            f"الاكتشاف: {discover[0].status} — جدد {stats.get('created', 0)}، مرتبطة {stats.get('linked', 0)}، "
            f"مراجعة تكرار {stats.get('review', 0)}، فرص {stats.get('opportunities', 0)}"
        )
    else:
        lines.append("الاكتشاف: لم تعمل دورة اليوم")
    lines.append("الفرص: " + ("، ".join(f"{k} {v}" for k, v in opp_status.items()) or "لا تغييرات"))
    lines.append("القرارات: " + ("، ".join(f"{k} {v}" for k, v in decisions.items()) or "لا شيء"))
    lines.append(
        f"رسائل مرسلة: {sent} · ردود: "
        + ("، ".join(f"{k} {v}" for k, v in replies.items()) or "لا شيء")
    )
    lines.append(
        "التكلفة التقديرية: "
        + ("، ".join(f"{k} {Decimal(v):.4f}" for k, v in cost.items()) or "0 (لا بيانات استهلاك)")
    )
    lines.append(f"بانتظار الاعتماد: {pending}")
    lines.append("العوائق: " + ("؛ ".join(blockers) if blockers else "لا شيء"))
    lines.append(
        "الخطوة التالية: " + ("راجعوا المسودات المعلقة" if pending else "لا إجراء مطلوب الآن")
    )
    content["text"] = "\n".join(lines)
    return content


async def run_digest(deps: Deps, workspace_id: uuid.UUID) -> dict[str, Any]:
    tz = await workspace_tz(deps.sm, workspace_id, deps.settings.app_timezone)
    day = local_today(tz)
    content = await build_digest(deps, workspace_id, day, tz)
    async with deps.sm() as db, db.begin():
        await db.execute(
            insert(Digest)
            .values(
                workspace_id=workspace_id,
                local_date=day,
                content=content,
                body_text=content["text"],
            )
            .on_conflict_do_nothing()
        )
        row = (
            await db.execute(
                select(Digest)
                .where(Digest.workspace_id == workspace_id, Digest.local_date == day)
                .with_for_update()
            )
        ).scalar_one()
        if row.sent_telegram_at is not None:
            return {"status": "already_sent"}
        row.content, row.body_text = content, content["text"]
    sent = await send_text(deps, workspace_id, content["text"])
    if sent:
        async with deps.sm() as db, db.begin():
            row = (
                await db.execute(
                    select(Digest)
                    .where(Digest.workspace_id == workspace_id, Digest.local_date == day)
                    .with_for_update()
                )
            ).scalar_one()
            row.sent_telegram_at = datetime.now(UTC)
    return {"status": "sent" if sent else "stored"}
