"""إعدادات التكاملات والتشغيل لكل workspace (غير سرية)، مع احتياط من ملف البيئة.

الأسرار منفصلة في app/services/secrets.py. في بيئة demo تُفرض الموصلات الاصطناعية مهما كان الإعداد.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import VersionConflict
from app.config import Settings
from app.db.models_ops import Integration
from app.services import audit

ModelProvider = Literal["fake", "anthropic", "openai", "gemini", "openai_compatible"]
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------- النماذج


class ModelRole(_Cfg):
    provider: ModelProvider = "fake"
    model: str = Field(default="", max_length=200)


class ModelPrice(_Cfg):
    input_per_mtok: Decimal = Field(ge=0, le=10000)
    output_per_mtok: Decimal = Field(ge=0, le=10000)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    pricing_version: str = Field(default="", max_length=40)


class ModelsConfig(_Cfg):
    extractor: ModelRole = ModelRole()
    writer: ModelRole = ModelRole()
    openai_compatible_base_url: str = Field(default="", max_length=500)
    openai_compatible_name: str = Field(default="", max_length=100)
    # الأسعار مفتاحها "provider:model". بلا سعر معرّف يُرفض الاستدعاء (لا تكلفة مجهولة غير محدودة).
    prices: dict[str, ModelPrice] = Field(default_factory=dict)
    max_output_tokens: int = Field(default=4000, ge=256, le=16000)
    # إقرار المالك بأن سياسة معالجة البيانات ومنطقتها لدى المزود المختار معتمدة (SPEC §10، §13).
    processing_approved: bool = False
    processing_note: str = Field(default="", max_length=1000)

    @field_validator("openai_compatible_base_url")
    @classmethod
    def _base_url(cls, v: str) -> str:
        v = v.strip()
        if v and not v.startswith("https://"):
            raise ValueError("رابط المزود المتوافق يجب أن يبدأ بـ https://")
        return v.rstrip("/")

    def price_for(self, provider: str, model: str) -> ModelPrice | None:
        return self.prices.get(f"{provider}:{model}")


# أسعار Anthropic الرسمية وقت البناء (USD لكل مليون توكن) للتعبئة المسبقة فقط؛ المالك يؤكدها.
SUGGESTED_PRICES: dict[str, tuple[str, str]] = {
    "anthropic:claude-opus-5": ("5.00", "25.00"),
    "anthropic:claude-sonnet-5": ("2.00", "10.00"),
    "anthropic:claude-haiku-4-5": ("1.00", "5.00"),
    "anthropic:claude-fable-5-1": ("10.00", "50.00"),
    "anthropic:claude-opus-4-8": ("5.00", "25.00"),
    "anthropic:claude-sonnet-4-6": ("3.00", "15.00"),
}
SUGGESTED_PRICES_VERSION = "anthropic-2026-06-24"


# ---------------------------------------------------------------- البريد


class MailConfig(_Cfg):
    # smtp: إرسال SMTP + قراءة IMAP. hostinger_api: إرسال عبر Hostinger Mail API (HTTPS) + قراءة IMAP.
    provider: Literal["fake", "smtp", "hostinger_api"] = "fake"
    preset: Literal["hostinger", "custom"] = "hostinger"
    smtp_host: str = Field(default="smtp.hostinger.com", max_length=253)
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_security: Literal["ssl", "starttls"] = "ssl"
    smtp_username: str = Field(default="", max_length=320)
    imap_enabled: bool = True
    imap_host: str = Field(default="imap.hostinger.com", max_length=253)
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_username: str = Field(default="", max_length=320)
    imap_same_password: bool = True
    imap_folder: str = Field(default="INBOX", max_length=200)
    sent_folder: str = Field(default="", max_length=200)
    from_address: str = Field(default="", max_length=320)
    from_name: str = Field(default="", max_length=200)
    reply_to: str = Field(default="", max_length=320)
    inbound_mode: Literal["imap_poll", "webhook_and_imap"] = "webhook_and_imap"
    sync_interval_minutes: int = Field(default=10, ge=2, le=120)
    daily_send_limit: int = Field(default=20, ge=1, le=500)
    signature: str = Field(default="", max_length=1000)
    opt_out_line: str = Field(
        default="إن لم يكن هذا مناسبًا لكم، يكفي الرد بكلمة «إيقاف» ولن نراسلكم مجددًا.",
        max_length=300,
    )
    booking_link: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _defaults(self) -> MailConfig:
        if self.preset == "hostinger":
            self.smtp_host = self.smtp_host or "smtp.hostinger.com"
            self.imap_host = self.imap_host or "imap.hostinger.com"
        if not self.imap_username:
            self.imap_username = self.smtp_username
        if not self.from_address:
            self.from_address = self.smtp_username
        return self

    @property
    def configured(self) -> bool:
        if self.provider == "hostinger_api":
            return bool(self.smtp_username and self.from_address)
        return self.provider == "smtp" and bool(
            self.smtp_host and self.smtp_username and self.from_address
        )


# ---------------------------------------------------------------- تيليجرام والبحث


class TelegramConfig(_Cfg):
    enabled: bool = False
    chat_id: str = Field(default="", max_length=40)
    mode: Literal["polling", "webhook"] = "polling"
    webhook_url: str = Field(default="", max_length=500)
    # مساعد الأسئلة: الأعضاء المربوطون فقط، قراءة فقط، ضمن الميزانية.
    assistant_enabled: bool = True
    assistant_daily_limit: int = Field(default=30, ge=1, le=500)

    @field_validator("chat_id")
    @classmethod
    def _chat(cls, v: str) -> str:
        v = v.strip()
        if v and not re.fullmatch(r"-?\d{3,20}", v):
            raise ValueError("معرف المجموعة رقم (يبدأ غالبًا بـ -100)")
        return v


class SearchConfig(_Cfg):
    provider: Literal["fake", "brave", "tavily"] = "fake"
    country: str = Field(default="SA", min_length=2, max_length=2)
    search_lang: str = Field(default="ar", min_length=2, max_length=10)
    max_queries: int = Field(default=3, ge=1, le=5)
    max_candidates: int = Field(default=20, ge=1, le=50)
    # سعر الطلب الواحد (لكل 1000 طلب) كما في خطة المالك؛ بلا سعر يُرفض البحث المدفوع.
    price_per_1k_requests: Decimal | None = Field(default=None, ge=0, le=1000)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    # خرائط Google (Places API New): مؤشر اكتشاف؛ نخزن place ID فقط.
    maps_provider: Literal["fake", "google"] = "fake"
    maps_results_per_query: int = Field(default=20, ge=1, le=20)
    # سعر كل 1000 طلب Text Search Enterprise بعد الحصة المجانية (0 إن بقيت ضمنها).
    maps_price_per_1k_requests: Decimal | None = Field(default=None, ge=0, le=1000)


# ---------------------------------------------------------------- التشغيل


class OperationsConfig(_Cfg):
    operating_mode: Literal["demo", "live"] = "demo"
    daily_budget: Decimal | None = Field(default=None, ge=0, le=100000)
    monthly_budget: Decimal | None = Field(default=None, ge=0, le=1000000)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    # سقوف اختيارية لكل فئة داخل السقف اليومي الكلي.
    discovery_daily_cap: Decimal | None = Field(default=None, ge=0)
    new_opportunity_daily_cap: Decimal | None = Field(default=None, ge=0)
    followup_daily_cap: Decimal | None = Field(default=None, ge=0)
    max_qualified_per_day: int = Field(default=3, ge=1, le=10)
    discovery_time: str = "09:00"
    process_slots: list[str] = Field(default_factory=lambda: ["10:00", "12:00", "14:00"])
    digest_time: str = "18:00"
    grace_hours: int = Field(default=3, ge=1, le=12)
    score_threshold: int = Field(default=70, ge=0, le=100)
    first_contact_cooldown_days: int = Field(default=14, ge=0, le=365)
    approval_expiry_hours: int = Field(default=48, ge=1, le=720)
    max_model_calls_per_opportunity: int = Field(default=5, ge=2, le=10)
    max_enrichment_pages: int = Field(default=3, ge=0, le=10)
    pause_discovery: bool = False
    pause_outbound: bool = False
    pause_all_processing: bool = False

    @field_validator("discovery_time", "digest_time")
    @classmethod
    def _time(cls, v: str) -> str:
        if not _TIME_RE.match(v):
            raise ValueError("الوقت بصيغة HH:MM (24 ساعة)")
        return v

    @field_validator("process_slots")
    @classmethod
    def _slots(cls, v: list[str]) -> list[str]:
        v = sorted({s.strip() for s in v if s.strip()})
        if not v or len(v) > 6 or not all(_TIME_RE.match(s) for s in v):
            raise ValueError("نوافذ المعالجة أوقات HH:MM (من 1 إلى 6)")
        return v


CONFIG_TYPES: dict[str, type[_Cfg]] = {
    "models": ModelsConfig,
    "mail": MailConfig,
    "telegram": TelegramConfig,
    "search": SearchConfig,
    "operations": OperationsConfig,
}


def _env_defaults(settings: Settings, type_: str) -> dict[str, Any]:
    """قيم ملف البيئة الاحتياطية للتكاملات عند غياب إعداد محفوظ."""
    if type_ == "mail" and settings.smtp_host:
        out: dict[str, Any] = {
            # وجود رمز Hostinger API في البيئة يعني الإرسال عبره (منافذ SMTP قد تكون ممنوعة).
            "provider": "hostinger_api"
            if settings.hostinger_mail_api_key.get_secret_value()
            else "smtp",
            "preset": "hostinger" if "hostinger" in settings.smtp_host else "custom",
            "smtp_host": settings.smtp_host,
            "smtp_username": settings.smtp_username,
            "from_address": settings.mail_from_address or settings.smtp_username,
            "from_name": settings.mail_from_name,
        }
        if settings.smtp_port:
            out["smtp_port"] = settings.smtp_port
        if settings.smtp_security in ("ssl", "starttls"):
            out["smtp_security"] = settings.smtp_security
        if settings.imap_host:
            out["imap_host"] = settings.imap_host
        if settings.imap_port:
            out["imap_port"] = settings.imap_port
        if settings.imap_username:
            out["imap_username"] = settings.imap_username
        if settings.imap_password.get_secret_value():
            out["imap_same_password"] = False
        return out
    if type_ == "telegram" and settings.telegram_bot_token.get_secret_value():
        return {"enabled": True, "chat_id": settings.telegram_chat_id}
    if type_ == "models" and settings.openai_compatible_base_url:
        return {"openai_compatible_base_url": settings.openai_compatible_base_url}
    return {}


async def _row(db: AsyncSession, workspace_id: uuid.UUID, type_: str) -> Integration | None:
    return (
        await db.execute(
            select(Integration).where(
                Integration.workspace_id == workspace_id, Integration.type == type_
            )
        )
    ).scalar_one_or_none()


async def get_config[C: _Cfg](
    db: AsyncSession, settings: Settings, workspace_id: uuid.UUID, type_: str, model: type[C]
) -> C:
    row = await _row(db, workspace_id, type_)
    data = dict(row.config) if row is not None and row.config else _env_defaults(settings, type_)
    try:
        return model.model_validate(data)
    except ValueError:
        return model()


async def get_version(db: AsyncSession, workspace_id: uuid.UUID, type_: str) -> int:
    row = await _row(db, workspace_id, type_)
    return row.version if row is not None else 0


async def save_config(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    type_: str,
    config: _Cfg,
    *,
    version: int,
    actor_id: str,
) -> int:
    """حفظ بتحقق تفاؤلي من الإصدار (0 = لم يُحفظ من قبل). يعيد الإصدار الجديد."""
    row = await _row(db, workspace_id, type_)
    current = row.version if row is not None else 0
    if version != current:
        raise VersionConflict()
    data = config.model_dump(mode="json")
    if row is None:
        await db.execute(
            insert(Integration).values(
                workspace_id=workspace_id, type=type_, config=data, version=1, updated_by=actor_id
            )
        )
        new_version = 1
    else:
        row.config = data
        row.version = current + 1
        row.updated_by = actor_id
        new_version = row.version
    await db.flush()
    audit.record(
        db,
        workspace_id=workspace_id,
        actor_id=actor_id,
        action=f"integration.{type_}.save",
        entity_type="integration",
        entity_version=new_version,
        change=_redacted(data),
    )
    return new_version


def _redacted(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k not in ("signature",)}


async def record_health(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    type_: str,
    *,
    ok: bool,
    message: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """يسجل نتيجة آخر اختبار ويحافظ على الحقول الأخرى (مثل قائمة النماذج المتاحة)."""
    row = await _row(db, workspace_id, type_)
    health = dict(row.health or {}) if row is not None else {}
    health.update({"ok": ok, "message": message[:500], "checked_at": datetime.now(UTC).isoformat()})
    if extra:
        health.update(extra)
    if row is None:
        await db.execute(
            insert(Integration).values(workspace_id=workspace_id, type=type_, health=health)
        )
    else:
        row.health = health
    await db.flush()


async def record_subhealth(
    db: AsyncSession, workspace_id: uuid.UUID, type_: str, key: str, *, ok: bool, message: str
) -> None:
    """نتيجة اختبار فرعي داخل صف تكامل قائم (مثل خرائط Google ضمن «search») دون مس نتيجته الأساسية."""
    row = await _row(db, workspace_id, type_)
    health = dict(row.health or {}) if row is not None else {}
    health[key] = {"ok": ok, "message": message[:500], "checked_at": datetime.now(UTC).isoformat()}
    if row is None:
        await db.execute(
            insert(Integration).values(workspace_id=workspace_id, type=type_, health=health)
        )
    else:
        row.health = health
    await db.flush()


async def get_health(db: AsyncSession, workspace_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    rows = (
        await db.execute(select(Integration).where(Integration.workspace_id == workspace_id))
    ).scalars()
    return {r.type: dict(r.health or {}) for r in rows}


async def get_cursor(db: AsyncSession, workspace_id: uuid.UUID, type_: str) -> dict[str, Any]:
    row = await _row(db, workspace_id, type_)
    return dict(row.last_sync_cursor or {}) if row is not None else {}


async def set_cursor(
    db: AsyncSession, workspace_id: uuid.UUID, type_: str, cursor: dict[str, Any]
) -> None:
    row = await _row(db, workspace_id, type_)
    if row is None:
        await db.execute(
            insert(Integration).values(
                workspace_id=workspace_id, type=type_, last_sync_cursor=cursor
            )
        )
    else:
        row.last_sync_cursor = cursor
    await db.flush()


def forced_fake(settings: Settings) -> bool:
    """بيئة demo لا تنفق مالًا ولا ترسل: كل التكاملات اصطناعية مهما كان الإعداد."""
    return settings.app_env.value == "demo"
