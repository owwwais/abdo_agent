"""التنظيف اليومي وسياسة الاحتفاظ (SPEC §13، T22): انتهاء الاعتمادات والحجوزات، حذف الأدلة المنتهية
ونتائج الفحص القديمة، وإلغاء معاينات الاستيراد المنتهية. الأدلة المحذوفة لا تُستعمل من أي مكان آخر لأن
التأهيل يقرأ الأدلة من القاعدة بشرط expires_at في كل تشغيل، وحالة الرسم لا تُعتمد مصدرًا للأدلة."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, update

from app.db.models import ImportBatch, ImportRow, SourceCheck
from app.db.models_sales import Evidence, InboundEvent, TelegramCallback, TelegramLinkCode
from app.services import approvals, budget
from app.workflows.common import Deps


async def run_cleanup(deps: Deps) -> dict[str, Any]:
    now = datetime.now(UTC)
    out: dict[str, Any] = {}
    async with deps.sm() as db, db.begin():
        out["approvals_expired"] = await approvals.expire_approvals(db)
        out["reservations_released"] = await budget.expire_stale(db)
        res = await db.execute(
            delete(Evidence).where(Evidence.expires_at < now).returning(Evidence.id)
        )
        out["evidence_deleted"] = len(res.all())
        res = await db.execute(
            delete(SourceCheck).where(SourceCheck.expires_at < now).returning(SourceCheck.id)
        )
        out["source_checks_deleted"] = len(res.all())
        expired = list(
            (
                await db.execute(
                    update(ImportBatch)
                    .where(ImportBatch.status == "preview", ImportBatch.expires_at < now)
                    .values(status="canceled")
                    .returning(ImportBatch.id)
                )
            ).scalars()
        )
        if expired:
            await db.execute(
                update(ImportRow).where(ImportRow.batch_id.in_(expired)).values(data=None)
            )
        out["previews_expired"] = len(expired)
        await db.execute(
            delete(TelegramCallback).where(TelegramCallback.expires_at < now - timedelta(days=7))
        )
        await db.execute(
            delete(TelegramLinkCode).where(TelegramLinkCode.expires_at < now - timedelta(days=1))
        )
        await db.execute(
            delete(InboundEvent).where(InboundEvent.received_at < now - timedelta(days=30))
        )
    return out
