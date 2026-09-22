"""اختبارات الاتصال من صفحة الإعدادات، وجاهزية التشغيل الفعلي. كل اختبار يسجل نتيجته في health
دون أي قيمة سرية. اختبارات النماذج والبحث المدفوعة صغيرة جدًا وتُسجل في سجل الاستهلاك."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.gateway import _KEY_FOR, CallCounter, ModelGateway, default_client_factory
from app.agents.providers import FakeClient, ProviderError
from app.api.errors import AppError
from app.config import Settings
from app.connectors.mail import OutgoingMail
from app.connectors.search import SearchError
from app.connectors.telegram import TelegramError
from app.db.models import Product, Source
from app.services import budget
from app.services import integrations as integ
from app.services.channels import ChannelNotReady, mail_context, search_context, telegram_context
from app.services.secrets import get_secret


class Ping(BaseModel):
    ok: bool
    reply: str = Field(max_length=100)


FakeClient.responders.setdefault("connection_ping", lambda user, ctx: {"ok": True, "reply": "pong"})


async def fetch_models(
    db: AsyncSession, settings: Settings, workspace_id: uuid.UUID, provider: str
) -> list[str]:
    if provider not in _KEY_FOR:
        raise AppError("مزود غير معروف", code="unknown_provider")
    cfg = await integ.get_config(db, settings, workspace_id, "models", integ.ModelsConfig)
    key, _ = await get_secret(db, settings, workspace_id, _KEY_FOR[provider])
    if not key:
        raise AppError(f"أدخل مفتاح {provider} واحفظه أولًا", code="api_key_missing")
    if provider == "openai_compatible" and not cfg.openai_compatible_base_url:
        raise AppError("أدخل رابط المزود المتوافق أولًا", code="base_url_missing")
    client = default_client_factory(provider, key, cfg)
    try:
        models = await client.list_models()
    except ProviderError as exc:
        await integ.record_health(
            db, workspace_id, "models", ok=False, message=f"{provider}: {exc}"
        )
        raise AppError(str(exc), code=exc.code) from exc
    row_health = (await integ.get_health(db, workspace_id)).get("models", {})
    available = dict(row_health.get("available", {}))
    available[provider] = models[:300]
    await integ.record_health(
        db,
        workspace_id,
        "models",
        ok=True,
        message=f"{provider}: {len(models)} نموذج متاح",
        extra={"available": available},
    )
    return models


async def test_model_role(
    settings: Settings, sm: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID, role: str
) -> str:
    gateway = ModelGateway(settings, sm, workspace_id)
    try:
        result, usage = await gateway.complete(
            role="writer" if role == "writer" else "extractor",
            system="أنت أداة اختبار اتصال. أعد JSON فقط.",
            user='أعد {"ok": true, "reply": "pong"} حرفيًا.',
            output_model=Ping,
            schema_name="connection_ping",
            category="test",
            counter=CallCounter(2),
            max_output_tokens=600,
            prompt_version="ping-v1",
        )
    except (ProviderError, AppError) as exc:
        async with sm() as db, db.begin():
            await integ.record_health(
                db, workspace_id, "models", ok=False, message=f"{role}: {exc}"
            )
        raise AppError(str(exc), code=getattr(exc, "code", "model_test_failed")) from exc
    message = (
        f"{role}: {usage.provider}/{usage.model} يعمل — {usage.input_tokens}+{usage.output_tokens} توكن، "
        f"تكلفة تقديرية {usage.cost.quantize(Decimal('0.000001'))}"
    )
    async with sm() as db, db.begin():
        await integ.record_health(db, workspace_id, "models", ok=result.ok, message=message)
    return message


async def test_mail(db: AsyncSession, settings: Settings, workspace_id: uuid.UUID) -> str:
    try:
        ctx = await mail_context(db, settings, workspace_id)
    except ChannelNotReady as exc:
        raise AppError(str(exc), code=exc.code) from exc
    report = await ctx.mailbox.test()
    await integ.record_health(db, workspace_id, "mail", ok=report.ok, message=report.summary())
    if not report.ok:
        raise AppError(report.summary(), code="mail_test_failed")
    return report.summary()


async def send_test_mail(
    db: AsyncSession, settings: Settings, workspace_id: uuid.UUID, to_address: str
) -> str:
    """رسالة اختبار إلى بريد المالك نفسه فقط (لا عملاء)."""
    if integ.forced_fake(settings):
        raise AppError("بيئة demo لا ترسل بريدًا حقيقيًا", code="demo_env")
    try:
        ctx = await mail_context(db, settings, workspace_id)
    except ChannelNotReady as exc:
        raise AppError(str(exc), code=exc.code) from exc
    outcome = await ctx.mailbox.send(
        OutgoingMail(
            from_address=ctx.config.from_address,
            from_name=ctx.config.from_name,
            to_address=to_address,
            subject="رسالة اختبار من وكيل المبيعات",
            body="هذه رسالة اختبار لإعدادات البريد. لا تحتاج أي إجراء.",
        )
    )
    ok = outcome.status == "sent"
    msg = f"{outcome.detail} — نسخة في المرسل: {'نعم' if outcome.sent_copy_saved else 'لا'}"
    await integ.record_health(db, workspace_id, "mail", ok=ok, message=f"رسالة اختبار: {msg}")
    if not ok:
        raise AppError(f"لم تُرسل رسالة الاختبار: {outcome.detail}", code="mail_send_failed")
    return msg


async def test_telegram(
    db: AsyncSession, settings: Settings, workspace_id: uuid.UUID, *, send: bool
) -> str:
    ctx = await telegram_context(db, settings, workspace_id)
    if ctx.client is None:
        raise AppError("فعّل تيليجرام واحفظ رمز البوت أولًا", code="telegram_not_configured")
    try:
        me = await ctx.client.get_me()
        message = f"البوت @{me.get('username', '?')} يعمل"
        if send:
            if not ctx.config.chat_id:
                raise AppError("أدخل معرف المجموعة أولًا", code="chat_id_missing")
            await ctx.client.send_message(
                ctx.config.chat_id, "✅ اختبار: البوت متصل بوكيل المبيعات."
            )
            message += "، وأُرسلت رسالة اختبار للمجموعة"
    except TelegramError as exc:
        await integ.record_health(db, workspace_id, "telegram", ok=False, message=str(exc))
        raise AppError(str(exc), code=exc.code) from exc
    await integ.record_health(db, workspace_id, "telegram", ok=True, message=message)
    return message


async def test_search(db: AsyncSession, settings: Settings, workspace_id: uuid.UUID) -> str:
    try:
        ctx = await search_context(db, settings, workspace_id)
    except ChannelNotReady as exc:
        raise AppError(str(exc), code=exc.code) from exc
    try:
        hits = await ctx.client.search("عيادات أسنان الرياض", count=3)
    except SearchError as exc:
        await integ.record_health(db, workspace_id, "search", ok=False, message=str(exc))
        raise AppError(str(exc), code=exc.code) from exc
    if ctx.client.paid:
        budget.record_usage(
            db,
            workspace_id=workspace_id,
            run_id=None,
            opportunity_id=None,
            category="test",
            kind="search",
            provider=ctx.client.name,
            model="",
            role="search",
            input_tokens=0,
            output_tokens=0,
            requests=1,
            estimated_cost=ctx.price_per_request,
            currency=ctx.config.currency,
            pricing_version="owner-set",
        )
    message = f"{ctx.client.name}: {len(hits)} نتيجة" + (
        " (اصطناعية)" if hits and hits[0].synthetic else ""
    )
    await integ.record_health(db, workspace_id, "search", ok=True, message=message)
    return message


async def live_readiness(
    db: AsyncSession, settings: Settings, workspace_id: uuid.UUID
) -> dict[str, list[str]]:
    """شروط التشغيل الفعلي: blockers تمنع التحويل إلى live، وwarnings تنبيهات لا تمنعه."""
    blockers: list[str] = []
    warnings: list[str] = []
    if integ.forced_fake(settings):
        blockers.append(
            "البيئة demo؛ التشغيل الفعلي يحتاج APP_ENV=development أو staging أو production"
        )
    ops = await integ.get_config(db, settings, workspace_id, "operations", integ.OperationsConfig)
    if ops.daily_budget is None or ops.monthly_budget is None:
        blockers.append("حدد الميزانية اليومية والشهرية")
    models = await integ.get_config(db, settings, workspace_id, "models", integ.ModelsConfig)
    for role_name, role in (("الاستخراج", models.extractor), ("التأهيل والصياغة", models.writer)):
        if role.provider == "fake" or not role.model:
            blockers.append(f"اختر مزودًا ونموذجًا حقيقيًا لدور {role_name}")
        elif models.price_for(role.provider, role.model) is None:
            blockers.append(f"أدخل سعر نموذج {role.model}")
        else:
            key, _ = await get_secret(db, settings, workspace_id, _KEY_FOR[role.provider])
            if not key:
                blockers.append(f"مفتاح {role.provider} غير مضبوط")
    if not models.processing_approved:
        blockers.append("أكد اعتماد سياسة معالجة البيانات لدى مزود النموذج")
    active_products = (
        await db.execute(
            select(func.count())
            .select_from(Product)
            .where(Product.workspace_id == workspace_id, Product.status == "active")
        )
    ).scalar_one()
    active_sources = (
        await db.execute(
            select(func.count())
            .select_from(Source)
            .where(Source.workspace_id == workspace_id, Source.status == "active")
        )
    ).scalar_one()
    if not active_products:
        blockers.append("فعّل منتجًا واحدًا على الأقل")
    if not active_sources:
        blockers.append("فعّل مصدرًا واحدًا على الأقل")
    mail = await integ.get_config(db, settings, workspace_id, "mail", integ.MailConfig)
    if (
        not mail.configured
        or not (await get_secret(db, settings, workspace_id, "smtp_password"))[0]
    ):
        warnings.append("البريد غير مهيأ: المسودات تُجهز لكن لا يُرسل بريد (واتساب اليدوي متاح)")
    if not settings.outbound_enabled:
        warnings.append("OUTBOUND_ENABLED=false في ملف البيئة: لن يُرسل أي بريد حتى بعد الاعتماد")
    search = await integ.get_config(db, settings, workspace_id, "search", integ.SearchConfig)
    if search.provider == "fake":
        warnings.append("البحث على الويب اصطناعي؛ الاكتشاف سيعتمد على المصادر الأخرى النشطة")
    return {"blockers": blockers, "warnings": warnings}


def health_badge(health: dict[str, Any]) -> tuple[str, str]:
    if not health or "ok" not in health:
        return ("muted", "لم يُختبر")
    return ("ok", "يعمل") if health.get("ok") else ("bad", "فشل آخر اختبار")
