"""نماذج ORM. كل كيان مملوك للشركة يحمل workspace_id، والعلاقات بين الكيانات المملوكة
تستخدم مفاتيح مركبة (workspace_id, id) تمنع ربط سجلات من workspaces مختلفة."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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

# ---------------------------------------------------------------- القيم المسموحة

OPERATING_MODES = ("demo", "live")
ROLES = ("owner", "reviewer")
MEMBERSHIP_STATUSES = ("active", "disabled")
AUTH_METHODS = ("supabase", "dev")
ACTOR_TYPES = ("user", "service", "system")

PRODUCT_STATUSES = ("draft", "active", "paused", "archived")
PRICE_STATUSES = ("needs_review", "approved")
SEGMENT_STATUSES = ("active", "archived")

SOURCE_KINDS = ("manual", "web_search", "html", "rss", "portal", "linkedin", "google_maps")
SOURCE_ACCESS_MODES = ("manual_only", "public_web", "api_key", "oauth")
SOURCE_STATUSES = (
    "new",
    "validating",
    "sample_ready",
    "active",
    "needs_setup",
    "restricted",
    "failed",
    "paused",
)
CHECK_STATUSES = ("succeeded", "needs_setup", "restricted", "failed")

COMPANY_STATUSES = ("active", "needs_review", "archived")
IDENTIFIER_STRENGTHS = ("strong", "weak")
DUPLICATE_STATUSES = ("open", "merged", "dismissed")
CONTACT_CHANNELS = ("email", "phone")
SUPPRESSION_SCOPES = ("company", "domain", "email", "phone")

IMPORT_KINDS = ("csv", "manual")
IMPORT_STATUSES = ("preview", "committed", "canceled")
IMPORT_ROW_ACTIONS = (
    "create",
    "link",
    "review",
    "skip_error",
    "skip_suppressed",
    "skip_duplicate_in_file",
    "skip_conflict",
)

JOB_STATUSES = ("queued", "running", "retry_scheduled", "completed", "failed", "canceled")


def _ws_fk(
    owner: str, col: str, table: str, *, ondelete: str | None = None
) -> ForeignKeyConstraint:
    """مفتاح أجنبي مركب (workspace_id, col) → table(workspace_id, id) باسم قصير صريح."""
    return ForeignKeyConstraint(
        ["workspace_id", col],
        [f"sales.{table}.workspace_id", f"sales.{table}.id"],
        name=f"fkw_{owner}_{col}"[:63],
        ondelete=ondelete,
    )


# ---------------------------------------------------------------- M0: الهوية والأساس


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (CheckConstraint(check_in("operating_mode", OPERATING_MODES), name="mode"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), server_default="Asia/Riyadh")
    operating_mode: Mapped[str] = mapped_column(String(16), server_default="demo")
    default_phone_region: Mapped[str | None] = mapped_column(String(2))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    settings_version: Mapped[int] = mapped_column(Integer, server_default="1")
    is_demo_data: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class UserProfile(Base):
    """ملف مستخدم بشري؛ auth_user_id هو sub من Supabase Auth أو معرف fixture للتطوير."""

    __tablename__ = "user_profiles"

    auth_user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    is_dev_fixture: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime] = created_at_col()


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("workspace_id", "auth_user_id"),
        CheckConstraint(check_in("role", ROLES), name="role"),
        CheckConstraint(check_in("status", MEMBERSHIP_STATUSES), name="status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    auth_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sales.user_profiles.auth_user_id"))
    role: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), server_default="active")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class WebSession(Base):
    """جلسة خادم. الكعكة تحمل رمزًا عشوائيًا؛ القاعدة تحفظ بصمته فقط."""

    __tablename__ = "web_sessions"
    __table_args__ = (CheckConstraint(check_in("auth_method", AUTH_METHODS), name="auth_method"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    auth_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sales.user_profiles.auth_user_id"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    csrf_token: Mapped[str] = mapped_column(String(64))
    auth_method: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = created_at_col()
    last_seen_at: Mapped[datetime] = created_at_col()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(check_in("actor_type", ACTOR_TYPES), name="actor_type"),
        Index(None, "workspace_id", text("occurred_at DESC")),
        Index(None, "entity_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[str | None] = mapped_column(String(50))
    entity_id: Mapped[uuid.UUID | None] = mapped_column()
    entity_version: Mapped[int | None] = mapped_column(Integer)
    redacted_change: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    occurred_at: Mapped[datetime] = created_at_col()


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    started_at: Mapped[datetime] = created_at_col()
    last_seen_at: Mapped[datetime] = created_at_col()
    current_job_id: Mapped[uuid.UUID | None] = mapped_column()
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base):
    """طابور PostgreSQL. payload يحمل مراجع صغيرة فقط، لا محتوى خام."""

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(check_in("status", JOB_STATUSES), name="status"),
        Index(
            "ix_jobs_claimable",
            "run_after",
            postgresql_where=text("status IN ('queued', 'retry_scheduled')"),
        ),
        Index(None, "workspace_id", "kind", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(20), server_default="queued")
    run_after: Mapped[datetime] = created_at_col()
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="3")
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- M1: المنتجات والفئات


class Segment(Base):
    __tablename__ = "segments"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "name"),
        CheckConstraint(check_in("status", SEGMENT_STATUSES), name="status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, server_default="")
    fit_rules: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    exclusion_rules: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(String(16), server_default="active")
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "name"),
        CheckConstraint(check_in("status", PRODUCT_STATUSES), name="status"),
        CheckConstraint(check_in("price_status", PRICE_STATUSES), name="price_status"),
        CheckConstraint("priority BETWEEN 1 AND 5", name="priority"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text, server_default="")
    problem: Mapped[str] = mapped_column(Text, server_default="")
    capabilities: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    unavailable_capabilities: Mapped[list[Any]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    fit_signals: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    exclusions: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    product_url: Mapped[str | None] = mapped_column(String(500))
    demo_url: Mapped[str | None] = mapped_column(String(500))
    price_status: Mapped[str] = mapped_column(String(16), server_default="needs_review")
    price_text: Mapped[str] = mapped_column(Text, server_default="")
    priority: Mapped[int] = mapped_column(Integer, server_default="3")
    status: Mapped[str] = mapped_column(String(16), server_default="draft")
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    is_demo_data: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_by: Mapped[str | None] = mapped_column(String(100))
    updated_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class ProductSegment(Base):
    __tablename__ = "product_segments"
    __table_args__ = (
        _ws_fk("product_segments", "product_id", "products", ondelete="CASCADE"),
        _ws_fk("product_segments", "segment_id", "segments", ondelete="CASCADE"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    segment_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    regions: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))


# ---------------------------------------------------------------- M1: المصادر


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "name"),
        CheckConstraint(check_in("kind", SOURCE_KINDS), name="kind"),
        CheckConstraint(check_in("access_mode", SOURCE_ACCESS_MODES), name="access_mode"),
        CheckConstraint(check_in("status", SOURCE_STATUSES), name="status"),
        CheckConstraint("max_pages BETWEEN 1 AND 20", name="max_pages"),
        CheckConstraint("max_records BETWEEN 1 AND 50", name="max_records"),
        CheckConstraint("max_requests BETWEEN 1 AND 50", name="max_requests"),
        CheckConstraint("timeout_seconds BETWEEN 5 AND 120", name="timeout"),
        CheckConstraint("retention_days BETWEEN 1 AND 3650", name="retention"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str | None] = mapped_column(String(2000))
    kind: Mapped[str] = mapped_column(String(20))
    connector_key: Mapped[str] = mapped_column(String(50))
    access_mode: Mapped[str] = mapped_column(String(20))
    allowed_hosts: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    allowed_paths: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    max_pages: Mapped[int] = mapped_column(Integer, server_default="3")
    max_records: Mapped[int] = mapped_column(Integer, server_default="5")
    max_requests: Mapped[int] = mapped_column(Integer, server_default="6")
    timeout_seconds: Mapped[int] = mapped_column(Integer, server_default="60")
    refresh_interval_hours: Mapped[int] = mapped_column(Integer, server_default="24")
    credential_ref: Mapped[str | None] = mapped_column(String(100))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    store_raw: Mapped[bool] = mapped_column(Boolean, server_default="false")
    retention_days: Mapped[int] = mapped_column(Integer, server_default="90")
    status: Mapped[str] = mapped_column(String(20), server_default="new")
    status_reason: Mapped[str | None] = mapped_column(Text)
    config_version: Mapped[int] = mapped_column(Integer, server_default="1")
    policy_version: Mapped[int] = mapped_column(Integer, server_default="0")
    policy_notes: Mapped[str] = mapped_column(Text, server_default="")
    policy_confirmed_by: Mapped[str | None] = mapped_column(String(100))
    policy_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    is_demo_data: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_by: Mapped[str | None] = mapped_column(String(100))
    updated_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class SourceSegment(Base):
    __tablename__ = "source_segments"
    __table_args__ = (
        _ws_fk("source_segments", "source_id", "sources", ondelete="CASCADE"),
        _ws_fk("source_segments", "segment_id", "segments", ondelete="CASCADE"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    segment_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    regions: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))


class SourceCheck(Base):
    __tablename__ = "source_checks"
    __table_args__ = (
        _ws_fk("source_checks", "source_id", "sources", ondelete="CASCADE"),
        CheckConstraint(check_in("status", CHECK_STATUSES), name="status"),
        Index(None, "source_id", text("checked_at DESC")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    source_id: Mapped[uuid.UUID] = mapped_column()
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sales.jobs.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(20))
    config_version: Mapped[int] = mapped_column(Integer)
    connector_key: Mapped[str] = mapped_column(String(50))
    sample_summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    allowed_fields: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    missing_fields: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    errors: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    request_count: Mapped[int] = mapped_column(Integer, server_default="0")
    bytes_fetched: Mapped[int] = mapped_column(Integer, server_default="0")
    duration_ms: Mapped[int] = mapped_column(Integer, server_default="0")
    # null = التكلفة غير معروفة؛ صفر = مجاني معروف (جلب HTTP مباشر).
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    cost_currency: Mapped[str | None] = mapped_column(String(3))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime] = created_at_col()


# ---------------------------------------------------------------- M1: الشركات وجهات الاتصال


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        _ws_fk("companies", "parent_id", "companies"),
        _ws_fk("companies", "segment_id", "segments"),
        CheckConstraint(check_in("status", COMPANY_STATUSES), name="status"),
        Index(None, "workspace_id", "normalized_name"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    display_name: Mapped[str] = mapped_column(String(300))
    normalized_name: Mapped[str] = mapped_column(String(300))
    domain: Mapped[str | None] = mapped_column(String(253))
    sector: Mapped[str | None] = mapped_column(String(200))
    region: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(200))
    country: Mapped[str | None] = mapped_column(String(2))
    segment_id: Mapped[uuid.UUID | None] = mapped_column()
    parent_id: Mapped[uuid.UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(16), server_default="active")
    notes: Mapped[str] = mapped_column(Text, server_default="")
    is_demo_data: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class CompanyIdentifier(Base):
    __tablename__ = "company_identifiers"
    __table_args__ = (
        _ws_fk("company_identifiers", "company_id", "companies", ondelete="CASCADE"),
        CheckConstraint(check_in("strength", IDENTIFIER_STRENGTHS), name="strength"),
        # المعرف القوي لا يخص إلا شركة واحدة؛ الضعيف (هاتف، اسم) قد يتكرر بين فروع.
        Index(
            "uq_company_identifiers_strong",
            "workspace_id",
            "kind",
            "normalized_value",
            unique=True,
            postgresql_where=text("strength = 'strong'"),
        ),
        Index(None, "workspace_id", "kind", "normalized_value"),
        UniqueConstraint("company_id", "kind", "normalized_value"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    company_id: Mapped[uuid.UUID] = mapped_column()
    kind: Mapped[str] = mapped_column(String(30))
    normalized_value: Mapped[str] = mapped_column(String(500))
    strength: Mapped[str] = mapped_column(String(10))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class CompanySourceLink(Base):
    """من أين عرفنا الشركة: مصدر + مرجع السجل. أساس ربط الأدلة لاحقًا."""

    __tablename__ = "company_source_links"
    __table_args__ = (
        _ws_fk("company_source_links", "company_id", "companies", ondelete="CASCADE"),
        _ws_fk("company_source_links", "source_id", "sources"),
        UniqueConstraint("company_id", "source_id", "source_record_ref"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    company_id: Mapped[uuid.UUID] = mapped_column()
    source_id: Mapped[uuid.UUID] = mapped_column()
    source_record_ref: Mapped[str] = mapped_column(String(500))
    url: Mapped[str | None] = mapped_column(String(2000))
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column()
    first_seen_at: Mapped[datetime] = created_at_col()
    last_seen_at: Mapped[datetime] = created_at_col()


class CompanyDuplicateCandidate(Base):
    __tablename__ = "company_duplicate_candidates"
    __table_args__ = (
        _ws_fk("company_duplicate_candidates", "company_id", "companies", ondelete="CASCADE"),
        _ws_fk(
            "company_duplicate_candidates", "candidate_company_id", "companies", ondelete="CASCADE"
        ),
        UniqueConstraint("company_id", "candidate_company_id"),
        CheckConstraint(check_in("status", DUPLICATE_STATUSES), name="status"),
        CheckConstraint("company_id <> candidate_company_id", name="distinct"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    company_id: Mapped[uuid.UUID] = mapped_column()
    candidate_company_id: Mapped[uuid.UUID] = mapped_column()
    reasons: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(String(16), server_default="open")
    decided_by: Mapped[str | None] = mapped_column(String(100))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class Contact(Base):
    """قناة تواصل مهنية. value محمية بعدم كشف المخطط وRLS؛ value_hash للمطابقة ومنع التواصل."""

    __tablename__ = "contacts"
    __table_args__ = (
        _ws_fk("contacts", "company_id", "companies", ondelete="CASCADE"),
        CheckConstraint(check_in("channel", CONTACT_CHANNELS), name="channel"),
        UniqueConstraint("workspace_id", "company_id", "channel", "value_hash"),
        Index(None, "workspace_id", "channel", "value_hash"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column()
    professional_name: Mapped[str | None] = mapped_column(String(200))
    role_title: Mapped[str | None] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(String(10))
    value: Mapped[str] = mapped_column(String(320))
    value_hash: Mapped[bytes] = mapped_column(LargeBinary)
    # False = الصيغة غير مؤكدة (مثل هاتف بلا دولة معروفة).
    normalized: Mapped[bool] = mapped_column(Boolean, server_default="false")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = created_at_col()


class Suppression(Base):
    __tablename__ = "suppressions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "scope", "target_hash"),
        CheckConstraint(check_in("scope", SUPPRESSION_SCOPES), name="scope"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    scope: Mapped[str] = mapped_column(String(16))
    target_hash: Mapped[bytes] = mapped_column(LargeBinary)
    reason: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = created_at_col()


# ---------------------------------------------------------------- M1: الاستيراد


class ImportBatch(Base):
    __tablename__ = "import_batches"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        _ws_fk("import_batches", "source_id", "sources"),
        CheckConstraint(check_in("kind", IMPORT_KINDS), name="kind"),
        CheckConstraint(check_in("status", IMPORT_STATUSES), name="status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    source_id: Mapped[uuid.UUID] = mapped_column()
    kind: Mapped[str] = mapped_column(String(10))
    filename: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(16), server_default="preview")
    row_count: Mapped[int] = mapped_column(Integer, server_default="0")
    error_count: Mapped[int] = mapped_column(Integer, server_default="0")
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = created_at_col()
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ImportRow(Base):
    __tablename__ = "import_rows"
    __table_args__ = (
        _ws_fk("import_rows", "batch_id", "import_batches", ondelete="CASCADE"),
        UniqueConstraint("batch_id", "row_number"),
        CheckConstraint(check_in("action", IMPORT_ROW_ACTIONS), name="action"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    batch_id: Mapped[uuid.UUID] = mapped_column()
    row_number: Mapped[int] = mapped_column(Integer)
    # بيانات الصف تُمسح بعد الاعتماد أو الإلغاء (تقليل البيانات).
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    display_name: Mapped[str] = mapped_column(String(300), server_default="")
    errors: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    match: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    action: Mapped[str] = mapped_column(String(30))
    result_company_id: Mapped[uuid.UUID | None] = mapped_column()
