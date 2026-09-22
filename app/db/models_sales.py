"""جداول مسار المبيعات: الأدلة، الفرص، أهلية التواصل، المسودات والاعتمادات، المحادثات والرسائل،
أوامر الإرسال، الأحداث الواردة، تيليجرام، والتواصل اليدوي."""

from __future__ import annotations

import uuid
from datetime import datetime
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
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, check_in, created_at_col, updated_at_col, uuid_pk

OPPORTUNITY_STATUSES = (
    "discovered",
    "qualified",
    "draft_ready",
    "contacted",
    "interested",
    "meeting_proposed",
    "not_interested",
    "won",
    "lost",
    "archived",
    "disqualified",
)
# الفرصة «نشطة» ما لم تكن في حالة نهائية؛ لا تتكرر فرصتان نشطتان لنفس الشركة والمنتج.
OPPORTUNITY_CLOSED = ("not_interested", "won", "lost", "archived", "disqualified")
PERMISSION_STATUSES = ("unknown", "allowed", "disallowed")
DRAFT_KINDS = ("first_contact", "reply", "followup")
DRAFT_STATUSES = (
    "pending_review",
    "approved",
    "rejected",
    "deferred",
    "stale",
    "sent",
    "canceled",
)
CHANNELS = ("email", "whatsapp")
APPROVAL_DECISIONS = ("approve", "reject", "defer")
APPROVAL_STATUSES = ("active", "superseded", "expired", "consumed")
OUTBOUND_STATUSES = ("pending", "sending", "sent", "unknown_delivery", "failed", "canceled")
MESSAGE_CLASSES = (
    "interested",
    "question",
    "meeting_request",
    "not_fit",
    "unsubscribe",
    "auto_reply",
    "bounce",
    "ambiguous",
)


def _ws_fk(
    owner: str, col: str, table: str, *, ondelete: str | None = None
) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["workspace_id", col],
        [f"sales.{table}.workspace_id", f"sales.{table}.id"],
        name=f"fkw_{owner}_{col}"[:63],
        ondelete=ondelete,
    )


def _ws() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("sales.workspaces.id", ondelete="CASCADE"))


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        _ws_fk("evidence", "company_id", "companies", ondelete="CASCADE"),
        _ws_fk("evidence", "source_id", "sources"),
        Index(None, "company_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _ws()
    company_id: Mapped[uuid.UUID] = mapped_column()
    source_id: Mapped[uuid.UUID | None] = mapped_column()
    url: Mapped[str | None] = mapped_column(String(2000))
    fact_type: Mapped[str] = mapped_column(String(30))
    claim: Mapped[str] = mapped_column(Text)
    # مقتطف قصير مسموح حفظه فقط (≤500 حرف)؛ لا HTML خام.
    permitted_excerpt: Mapped[str] = mapped_column(Text, server_default="")
    confidence_label: Mapped[str] = mapped_column(String(20), server_default="observed")
    storage_policy: Mapped[str] = mapped_column(String(20), server_default="extracted_only")
    observed_at: Mapped[datetime] = created_at_col()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Opportunity(Base):
    __tablename__ = "opportunities"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        _ws_fk("opportunities", "company_id", "companies", ondelete="CASCADE"),
        _ws_fk("opportunities", "product_id", "products"),
        _ws_fk("opportunities", "segment_id", "segments"),
        CheckConstraint(check_in("status", OPPORTUNITY_STATUSES), name="status"),
        Index(
            "uq_opportunities_active_pair",
            "workspace_id",
            "company_id",
            "product_id",
            unique=True,
            postgresql_where=text(
                "status NOT IN ('not_interested','won','lost','archived','disqualified')"
            ),
        ),
        Index(None, "workspace_id", "status", "next_action_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _ws()
    company_id: Mapped[uuid.UUID] = mapped_column()
    product_id: Mapped[uuid.UUID] = mapped_column()
    segment_id: Mapped[uuid.UUID | None] = mapped_column()
    source_id: Mapped[uuid.UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(20), server_default="discovered")
    preliminary_score: Mapped[int] = mapped_column(Integer, server_default="0")
    score: Mapped[int | None] = mapped_column(Integer)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    facts: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    hypotheses: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    missing_info: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    evidence_ids: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    selection_reason: Mapped[str] = mapped_column(Text, server_default="")
    disqualify_reason: Mapped[str | None] = mapped_column(Text)
    graph_thread_id: Mapped[str | None] = mapped_column(String(100))
    discovered_run_id: Mapped[uuid.UUID | None] = mapped_column()
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_demo_data: Mapped[bool] = mapped_column(Boolean, server_default="false")
    status_changed_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class ContactPermission(Base):
    __tablename__ = "contact_permissions"
    __table_args__ = (
        _ws_fk("contact_permissions", "contact_id", "contacts", ondelete="CASCADE"),
        UniqueConstraint("contact_id", "purpose"),
        CheckConstraint(check_in("status", PERMISSION_STATUSES), name="status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _ws()
    contact_id: Mapped[uuid.UUID] = mapped_column()
    purpose: Mapped[str] = mapped_column(String(30), server_default="sales_outreach")
    status: Mapped[str] = mapped_column(String(16), server_default="unknown")
    basis_reference: Mapped[str] = mapped_column(Text, server_default="")
    confirmed_by: Mapped[str | None] = mapped_column(String(100))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        _ws_fk("conversations", "company_id", "companies", ondelete="CASCADE"),
        _ws_fk("conversations", "opportunity_id", "opportunities"),
        Index(None, "workspace_id", "company_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _ws()
    company_id: Mapped[uuid.UUID] = mapped_column()
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column()
    contact_id: Mapped[uuid.UUID | None] = mapped_column()
    channel: Mapped[str] = mapped_column(String(10), server_default="email")
    provider: Mapped[str] = mapped_column(String(30))
    mailbox: Mapped[str | None] = mapped_column(String(320))
    provider_thread_id: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), server_default="open")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        Index(
            "uq_messages_internet_id",
            "workspace_id",
            "internet_message_id",
            unique=True,
            postgresql_where=text("internet_message_id IS NOT NULL"),
        ),
        CheckConstraint("direction IN ('inbound', 'outbound')", name="direction"),
        Index(None, "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _ws()
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sales.conversations.id", ondelete="CASCADE")
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column()
    direction: Mapped[str] = mapped_column(String(10))
    channel: Mapped[str] = mapped_column(String(10), server_default="email")
    provider_message_id: Mapped[str | None] = mapped_column(String(300))
    internet_message_id: Mapped[str | None] = mapped_column(String(500))
    in_reply_to: Mapped[str | None] = mapped_column(String(500))
    references: Mapped[str] = mapped_column(Text, server_default="")
    from_address: Mapped[str | None] = mapped_column(String(320))
    to_address: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(Text, server_default="")
    body_text: Mapped[str] = mapped_column(Text, server_default="")
    classification: Mapped[str | None] = mapped_column(String(20))
    classification_detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    link_status: Mapped[str] = mapped_column(String(20), server_default="linked")
    candidate_opportunity_ids: Mapped[list[Any]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    sent_or_received_at: Mapped[datetime] = created_at_col()
    created_at: Mapped[datetime] = created_at_col()


class Draft(Base):
    __tablename__ = "drafts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        _ws_fk("drafts", "opportunity_id", "opportunities", ondelete="CASCADE"),
        CheckConstraint(check_in("kind", DRAFT_KINDS), name="kind"),
        CheckConstraint(check_in("status", DRAFT_STATUSES), name="status"),
        CheckConstraint(check_in("channel", CHANNELS), name="channel"),
        Index(None, "workspace_id", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _ws()
    opportunity_id: Mapped[uuid.UUID] = mapped_column()
    conversation_id: Mapped[uuid.UUID | None] = mapped_column()
    reply_to_message_id: Mapped[uuid.UUID | None] = mapped_column()
    kind: Mapped[str] = mapped_column(String(20), server_default="first_contact")
    channel: Mapped[str] = mapped_column(String(10), server_default="email")
    revision: Mapped[int] = mapped_column(Integer, server_default="1")
    recipient_contact_id: Mapped[uuid.UUID | None] = mapped_column()
    recipient_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    subject: Mapped[str] = mapped_column(Text, server_default="")
    body: Mapped[str] = mapped_column(Text, server_default="")
    attachment_refs: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    content_hash: Mapped[str] = mapped_column(String(64))
    product_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), server_default="pending_review")
    defer_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    policy_findings: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    model_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_by: Mapped[str] = mapped_column(String(100))
    updated_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        _ws_fk("approvals", "draft_id", "drafts", ondelete="CASCADE"),
        CheckConstraint(check_in("decision", APPROVAL_DECISIONS), name="decision"),
        CheckConstraint(check_in("status", APPROVAL_STATUSES), name="status"),
        # قرار واحد فعال لكل إصدار من المسودة.
        Index(
            "uq_approvals_active_revision",
            "draft_id",
            "draft_revision",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _ws()
    draft_id: Mapped[uuid.UUID] = mapped_column()
    draft_revision: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(100))
    via: Mapped[str] = mapped_column(String(10), server_default="web")
    decision: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(12), server_default="active")
    note: Mapped[str] = mapped_column(Text, server_default="")
    decided_at: Mapped[datetime] = created_at_col()
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboundCommand(Base):
    __tablename__ = "outbound_commands"
    __table_args__ = (
        _ws_fk("outbound_commands", "draft_id", "drafts", ondelete="CASCADE"),
        UniqueConstraint("draft_id", "draft_revision"),
        UniqueConstraint("idempotency_key"),
        CheckConstraint(check_in("status", OUTBOUND_STATUSES), name="status"),
        Index(None, "workspace_id", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _ws()
    draft_id: Mapped[uuid.UUID] = mapped_column()
    draft_revision: Mapped[int] = mapped_column(Integer)
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sales.approvals.id"))
    idempotency_key: Mapped[str] = mapped_column(String(120))
    channel: Mapped[str] = mapped_column(String(10), server_default="email")
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    provider: Mapped[str | None] = mapped_column(String(30))
    internet_message_id: Mapped[str | None] = mapped_column(String(500))
    provider_message_id: Mapped[str | None] = mapped_column(String(300))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    reconciliation_needed: Mapped[bool] = mapped_column(Boolean, server_default="false")
    resolved_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InboundEvent(Base):
    __tablename__ = "inbound_events"
    __table_args__ = (UniqueConstraint("provider", "event_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sales.workspaces.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String(30))
    event_id: Mapped[str] = mapped_column(String(200))
    # حمولة مختصرة منقحة للتشخيص؛ المحتوى الكامل يُجلب من المزود عند المعالجة.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(20), server_default="received")
    received_at: Mapped[datetime] = created_at_col()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TelegramCallback(Base):
    """مرجع opaque قصير العمر لأزرار بطاقة الاعتماد؛ الزر لا يحمل أي بيانات يثق بها الخادم."""

    __tablename__ = "telegram_callbacks"

    token: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = _ws()
    draft_id: Mapped[uuid.UUID] = mapped_column()
    draft_revision: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(10))
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class TelegramLinkCode(Base):
    __tablename__ = "telegram_link_codes"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = _ws()
    auth_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales.user_profiles.auth_user_id", ondelete="CASCADE")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ManualContact(Base):
    """تسجيل تواصل يدوي (واتساب). النسخ وحده لا يعني الإرسال؛ هذا تأكيد المؤسس."""

    __tablename__ = "manual_contacts"
    __table_args__ = (_ws_fk("manual_contacts", "opportunity_id", "opportunities"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _ws()
    opportunity_id: Mapped[uuid.UUID] = mapped_column()
    draft_id: Mapped[uuid.UUID | None] = mapped_column()
    channel: Mapped[str] = mapped_column(String(10), server_default="whatsapp")
    contact_id: Mapped[uuid.UUID | None] = mapped_column()
    actor_id: Mapped[str] = mapped_column(String(100))
    note: Mapped[str] = mapped_column(Text, server_default="")
    contacted_at: Mapped[datetime] = created_at_col()
