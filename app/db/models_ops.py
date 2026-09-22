"""جداول التشغيل: التكاملات والأسرار المشفرة، التشغيلات، الاستهلاك والميزانية والحصص، والملخص اليومي."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, check_in, created_at_col, updated_at_col, uuid_pk

INTEGRATION_TYPES = ("models", "mail", "telegram", "search", "operations")
RUN_KINDS = (
    "discover",
    "process_opportunity",
    "process_reply",
    "digest",
    "mail_sync",
    "connection_test",
)
RUN_STATUSES = (
    "queued",
    "running",
    "completed",
    "waiting_approval",
    "retry_scheduled",
    "blocked_budget",
    "blocked_policy",
    "failed",
    "canceled",
    "skipped_configuration",
)
USAGE_CATEGORIES = ("discovery", "new_opportunity", "followup", "test")
RESERVATION_STATUSES = ("active", "settled", "released")


class Integration(Base):
    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "type"),
        CheckConstraint(check_in("type", INTEGRATION_TYPES), name="type"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    type: Mapped[str] = mapped_column(String(20))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # آخر نتيجة اختبار اتصال: {"ok": bool, "message": str, "checked_at": iso}
    health: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    last_sync_cursor: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    updated_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class IntegrationSecret(Base):
    """سر مشفر بـFernet. القيمة لا تُعرض ولا تُسجل؛ يظهر في الواجهة أنه «مضبوط» فقط."""

    __tablename__ = "integration_secrets"
    __table_args__ = (UniqueConstraint("workspace_id", "key"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    key: Mapped[str] = mapped_column(String(60))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    updated_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(check_in("kind", RUN_KINDS), name="kind"),
        CheckConstraint(check_in("status", RUN_STATUSES), name="status"),
        Index(None, "workspace_id", text("started_at DESC")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), server_default="running")
    graph_thread_id: Mapped[str | None] = mapped_column(String(100))
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column()
    job_id: Mapped[uuid.UUID | None] = mapped_column()
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    error_code: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = created_at_col()
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (Index(None, "run_id", "occurred_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sales.runs.id", ondelete="CASCADE"))
    step: Mapped[str] = mapped_column(String(60))
    event_type: Mapped[str] = mapped_column(String(30))
    sanitized_summary: Mapped[str] = mapped_column(Text, server_default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    occurred_at: Mapped[datetime] = created_at_col()


class UsageLedger(Base):
    __tablename__ = "usage_ledger"
    __table_args__ = (
        CheckConstraint(check_in("category", USAGE_CATEGORIES), name="category"),
        Index(None, "workspace_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sales.runs.id", ondelete="SET NULL")
    )
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column()
    category: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(20), server_default="model")  # model | search
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(120), server_default="")
    role: Mapped[str] = mapped_column(String(30), server_default="")
    input_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    request_count: Mapped[int] = mapped_column(Integer, server_default="1")
    pricing_version: Mapped[str] = mapped_column(String(40), server_default="")
    estimated_cost: Mapped[Decimal] = mapped_column(Numeric(14, 6), server_default="0")
    # null = لا بيانات فوترة من المزود؛ نعتمد التقدير من التوكنات المسجلة وسعر الإعداد.
    actual_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    currency: Mapped[str] = mapped_column(String(3), server_default="USD")
    occurred_at: Mapped[datetime] = created_at_col()


class BudgetReservation(Base):
    __tablename__ = "budget_reservations"
    __table_args__ = (
        CheckConstraint(check_in("status", RESERVATION_STATUSES), name="status"),
        Index(None, "workspace_id", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sales.runs.id", ondelete="SET NULL")
    )
    scope_key: Mapped[str] = mapped_column(String(20))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    settled_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    currency: Mapped[str] = mapped_column(String(3), server_default="USD")
    status: Mapped[str] = mapped_column(String(16), server_default="active")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class DailyQuota(Base):
    __tablename__ = "daily_quotas"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    local_date: Mapped[date] = mapped_column(Date, primary_key=True)
    discovery_count: Mapped[int] = mapped_column(Integer, server_default="0")
    qualified_slots_reserved: Mapped[int] = mapped_column(Integer, server_default="0")
    qualified_slots_used: Mapped[int] = mapped_column(Integer, server_default="0")


class Digest(Base):
    __tablename__ = "digests"
    __table_args__ = (UniqueConstraint("workspace_id", "local_date"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    local_date: Mapped[date] = mapped_column(Date)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    body_text: Mapped[str] = mapped_column(Text, server_default="")
    sent_telegram_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
