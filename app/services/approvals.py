"""المسودات والاعتمادات (SPEC §8.2–8.4، §8.6).

- الاعتماد مربوط بإصدار المسودة وبصمة (المستلم + الموضوع + النص + المرفقات + إصدار المنتج + القناة).
- تعديل أي جزء جوهري ينشئ إصدارًا جديدًا ويبطل الاعتماد السابق ويلغي أمر الإرسال المعلق.
- قفل صف المسودة + فهرس فريد لقرار فعال واحد لكل إصدار: ضغطتان متزامنتان → قرار واحد وأمر إرسال واحد.
- معاملة قبول الاعتماد تكتب أمر الإرسال (outbound_command) ولا ترسل شيئًا بنفسها.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import Conflict, Forbidden, InvalidInput, NotFound
from app.config import Settings
from app.db.models import Contact, Membership, Product
from app.db.models_sales import (
    Approval,
    ContactPermission,
    Conversation,
    Draft,
    ManualContact,
    Message,
    Opportunity,
    OutboundCommand,
)
from app.jobs import queue
from app.services import audit
from app.services import integrations as integ
from app.services.policy import blocking

Decision = Literal["approve", "reject", "defer"]


def compute_hash(
    recipient: dict[str, Any],
    subject: str,
    body: str,
    attachments: list[Any],
    product_version: int,
    channel: str,
) -> str:
    payload = json.dumps(
        {
            "r": recipient,
            "s": subject,
            "b": body,
            "a": attachments,
            "pv": product_version,
            "c": channel,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def recipient_snapshot(contact: Contact | None) -> dict[str, Any]:
    if contact is None:
        return {}
    return {
        "contact_id": str(contact.id),
        "channel": contact.channel,
        "value": contact.value,
        "name": contact.professional_name or "",
    }


async def eligibility(db: AsyncSession, contact_id: uuid.UUID | None) -> str:
    if contact_id is None:
        return "unknown"
    row = (
        await db.execute(
            select(ContactPermission).where(
                ContactPermission.contact_id == contact_id,
                ContactPermission.purpose == "sales_outreach",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return "unknown"
    if row.expires_at and row.expires_at < datetime.now(UTC):
        return "unknown"
    return row.status


async def create_draft(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    opportunity: Opportunity,
    kind: str,
    channel: str,
    contact: Contact | None,
    subject: str,
    body: str,
    product_version: int,
    findings: list[dict[str, Any]],
    model_meta: dict[str, Any],
    created_by: str,
    conversation_id: uuid.UUID | None = None,
    reply_to_message_id: uuid.UUID | None = None,
) -> Draft:
    snap = recipient_snapshot(contact)
    draft = Draft(
        workspace_id=workspace_id,
        opportunity_id=opportunity.id,
        kind=kind,
        channel=channel,
        revision=1,
        recipient_contact_id=contact.id if contact else None,
        recipient_snapshot=snap,
        subject=subject,
        body=body,
        attachment_refs=[],
        content_hash=compute_hash(snap, subject, body, [], product_version, channel),
        product_version=product_version,
        status="pending_review",
        policy_findings=findings,
        model_meta=model_meta,
        created_by=created_by,
        conversation_id=conversation_id,
        reply_to_message_id=reply_to_message_id,
    )
    db.add(draft)
    await db.flush()
    audit.record(
        db,
        workspace_id=workspace_id,
        actor_id=created_by,
        action="draft.create",
        entity_type="draft",
        entity_id=draft.id,
        entity_version=1,
        change={"kind": kind, "channel": channel},
    )
    return draft


async def _get_draft(
    db: AsyncSession, workspace_id: uuid.UUID, draft_id: uuid.UUID, *, lock: bool
) -> Draft:
    q = select(Draft).where(Draft.id == draft_id, Draft.workspace_id == workspace_id)
    if lock:
        q = q.with_for_update()
    draft = (await db.execute(q)).scalar_one_or_none()
    if draft is None:
        raise NotFound()
    return draft


async def _supersede(db: AsyncSession, draft: Draft, reason: str) -> None:
    await db.execute(
        update(Approval)
        .where(Approval.draft_id == draft.id, Approval.status == "active")
        .values(status="superseded", note=reason)
    )
    await db.execute(
        update(OutboundCommand)
        .where(OutboundCommand.draft_id == draft.id, OutboundCommand.status == "pending")
        .values(status="canceled", last_error=reason)
    )


async def edit_draft(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    actor_id: str,
    draft_id: uuid.UUID,
    revision: int,
    subject: str,
    body: str,
    contact_id: uuid.UUID | None = None,
    findings: list[dict[str, Any]] | None = None,
) -> Draft:
    draft = await _get_draft(db, workspace_id, draft_id, lock=True)
    if draft.revision != revision:
        raise Conflict("تغيّرت المسودة منذ فتحها؛ حدّث الصفحة", code="version_conflict")
    if draft.status in ("sent", "canceled"):
        raise Conflict("لا يمكن تعديل مسودة أُرسلت أو أُلغيت", code="draft_closed")
    if not subject.strip() or not body.strip():
        raise InvalidInput("الموضوع والنص مطلوبان")
    snap = dict(draft.recipient_snapshot)
    if contact_id is not None and str(contact_id) != snap.get("contact_id"):
        contact = (
            await db.execute(
                select(Contact).where(
                    Contact.id == contact_id, Contact.workspace_id == workspace_id
                )
            )
        ).scalar_one_or_none()
        if contact is None:
            raise NotFound("جهة الاتصال غير موجودة")
        snap = recipient_snapshot(contact)
        draft.recipient_contact_id = contact.id
        draft.channel = "email" if contact.channel == "email" else "whatsapp"
    new_hash = compute_hash(
        snap,
        subject.strip(),
        body.strip(),
        draft.attachment_refs,
        draft.product_version,
        draft.channel,
    )
    if new_hash == draft.content_hash:
        return draft
    await _supersede(db, draft, "عُدلت المسودة")
    draft.revision += 1
    draft.subject, draft.body, draft.recipient_snapshot, draft.content_hash = (
        subject.strip(),
        body.strip(),
        snap,
        new_hash,
    )
    draft.status = "pending_review"
    draft.updated_by = actor_id
    if findings is not None:
        draft.policy_findings = findings
    await db.flush()
    audit.record(
        db,
        workspace_id=workspace_id,
        actor_id=actor_id,
        action="draft.edit",
        entity_type="draft",
        entity_id=draft.id,
        entity_version=draft.revision,
        change={"recipient_changed": contact_id is not None},
    )
    return draft


async def decide(
    db: AsyncSession,
    settings: Settings,
    *,
    workspace_id: uuid.UUID,
    actor_id: str,
    auth_user_id: uuid.UUID,
    draft_id: uuid.UUID,
    revision: int,
    content_hash: str,
    decision: Decision,
    via: str = "web",
    note: str = "",
    defer_hours: int = 24,
) -> tuple[Approval, bool]:
    """يعيد (القرار، هل أُنشئ الآن). القرار المسجل مسبقًا للإصدار نفسه يُعاد بلا أثر إضافي."""
    member = (
        await db.execute(
            select(Membership).where(
                Membership.workspace_id == workspace_id,
                Membership.auth_user_id == auth_user_id,
                Membership.status == "active",
            )
        )
    ).scalar_one_or_none()
    if member is None or member.role not in ("owner", "reviewer"):
        raise Forbidden("لا تملك صلاحية اعتماد الرسائل", code="not_reviewer")
    draft = await _get_draft(db, workspace_id, draft_id, lock=True)
    existing = (
        await db.execute(
            select(Approval).where(
                Approval.draft_id == draft.id,
                Approval.draft_revision == revision,
                Approval.status.in_(("active", "consumed")),
                Approval.decision.in_(("approve", "reject")),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    if draft.revision != revision or draft.content_hash != content_hash:
        raise Conflict("المسودة تغيّرت بعد عرضها عليك؛ راجع الإصدار الأحدث", code="stale_revision")
    if draft.status not in ("pending_review", "deferred"):
        raise Conflict(f"حالة المسودة ({draft.status}) لا تسمح بالقرار", code="draft_not_pending")
    ops = await integ.get_config(db, settings, workspace_id, "operations", integ.OperationsConfig)
    now = datetime.now(UTC)
    if decision == "approve":
        if blocking(draft.policy_findings):
            raise Conflict("المسودة فيها مخالفات سياسة؛ عدّلها أولًا", code="policy_blocked")
        product = (
            await db.execute(
                select(Product)
                .join(Opportunity, Opportunity.product_id == Product.id)
                .where(Opportunity.id == draft.opportunity_id)
            )
        ).scalar_one()
        if product.status != "active" or product.version != draft.product_version:
            raise Conflict(
                "المنتج تغيّر أو أوقف منذ كتابة المسودة؛ أعد المراجعة", code="product_changed"
            )
        if (
            draft.channel == "email"
            and await eligibility(db, draft.recipient_contact_id) != "allowed"
        ):
            raise Conflict(
                "أهلية التواصل مع المستلم غير مؤكدة؛ أكدها مع السند أولًا",
                code="eligibility_unknown",
            )
    approval = Approval(
        workspace_id=workspace_id,
        draft_id=draft.id,
        draft_revision=revision,
        content_hash=content_hash,
        actor_id=actor_id,
        via=via,
        decision=decision,
        note=note[:1000],
        status="active" if decision == "approve" else "consumed",
        expires_at=now + timedelta(hours=ops.approval_expiry_hours)
        if decision == "approve"
        else None,
    )
    db.add(approval)
    await db.flush()
    if decision == "approve":
        draft.status = "approved"
        if draft.channel == "email":
            key = f"draft:{draft.id}:rev:{revision}"
            await db.execute(
                insert(OutboundCommand)
                .values(
                    workspace_id=workspace_id,
                    draft_id=draft.id,
                    draft_revision=revision,
                    approval_id=approval.id,
                    idempotency_key=key,
                    channel="email",
                    status="pending",
                )
                .on_conflict_do_nothing()
            )
            await queue.enqueue(
                db,
                kind="send_outbound",
                workspace_id=workspace_id,
                payload={"draft_id": str(draft.id), "revision": revision},
                idempotency_key=f"send:{key}",
                max_attempts=3,
            )
    elif decision == "reject":
        draft.status = "rejected"
    else:
        draft.status = "deferred"
        draft.defer_until = now + timedelta(hours=max(1, defer_hours))
    draft.updated_by = actor_id
    opp = await db.get(Opportunity, draft.opportunity_id)
    if opp is not None and opp.graph_thread_id and decision != "defer":
        await queue.enqueue(
            db,
            kind="resume_opportunity",
            workspace_id=workspace_id,
            payload={
                "thread_id": opp.graph_thread_id,
                "decision": decision,
                "draft_id": str(draft.id),
            },
            idempotency_key=f"resume:{opp.graph_thread_id}:{draft.id}:{revision}",
        )
    audit.record(
        db,
        workspace_id=workspace_id,
        actor_id=actor_id,
        action=f"draft.{decision}",
        entity_type="draft",
        entity_id=draft.id,
        entity_version=revision,
        change={"via": via},
    )
    return approval, True


async def set_eligibility(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    actor_id: str,
    contact_id: uuid.UUID,
    status: Literal["allowed", "disallowed", "unknown"],
    basis: str,
) -> None:
    contact = (
        await db.execute(
            select(Contact).where(Contact.id == contact_id, Contact.workspace_id == workspace_id)
        )
    ).scalar_one_or_none()
    if contact is None:
        raise NotFound("جهة الاتصال غير موجودة")
    if status == "allowed" and len(basis.strip()) < 5:
        raise InvalidInput("اذكر سند التواصل (مثل: بريد عمل منشور للتواصل التجاري، إحالة بموافقة)")
    now = datetime.now(UTC)
    stmt = insert(ContactPermission).values(
        workspace_id=workspace_id,
        contact_id=contact_id,
        purpose="sales_outreach",
        status=status,
        basis_reference=basis.strip()[:1000],
        confirmed_by=actor_id,
        confirmed_at=now,
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[ContactPermission.contact_id, ContactPermission.purpose],
            set_={
                "status": status,
                "basis_reference": stmt.excluded.basis_reference,
                "confirmed_by": actor_id,
                "confirmed_at": now,
            },
        )
    )
    audit.record(
        db,
        workspace_id=workspace_id,
        actor_id=actor_id,
        action="contact.eligibility",
        entity_type="contact",
        entity_id=contact_id,
        change={"status": status},
    )


async def ensure_conversation(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    opportunity: Opportunity,
    contact_id: uuid.UUID | None,
    channel: str,
    provider: str,
    mailbox: str | None,
) -> Conversation:
    conv = (
        await db.execute(
            select(Conversation)
            .where(
                Conversation.workspace_id == workspace_id,
                Conversation.opportunity_id == opportunity.id,
                Conversation.channel == channel,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if conv is None:
        conv = Conversation(
            workspace_id=workspace_id,
            company_id=opportunity.company_id,
            opportunity_id=opportunity.id,
            contact_id=contact_id,
            channel=channel,
            provider=provider,
            mailbox=mailbox,
            status="open",
        )
        db.add(conv)
        await db.flush()
    return conv


async def record_manual_contact(
    db: AsyncSession, *, workspace_id: uuid.UUID, actor_id: str, draft_id: uuid.UUID, note: str
) -> ManualContact:
    """واتساب يدوي: التأكيد البشري فقط يغير الحالة إلى «تم التواصل»؛ النسخ وحده لا يكفي."""
    draft = await _get_draft(db, workspace_id, draft_id, lock=True)
    if draft.channel != "whatsapp":
        raise InvalidInput("هذه المسودة ليست للتواصل اليدوي")
    if draft.status != "approved":
        raise Conflict("اعتمد المسودة أولًا ثم سجّل التواصل اليدوي", code="not_approved")
    opp = await db.get(Opportunity, draft.opportunity_id)
    assert opp is not None
    mc = ManualContact(
        workspace_id=workspace_id,
        opportunity_id=opp.id,
        draft_id=draft.id,
        channel="whatsapp",
        contact_id=draft.recipient_contact_id,
        actor_id=actor_id,
        note=note[:1000],
    )
    db.add(mc)
    conv = await ensure_conversation(
        db,
        workspace_id=workspace_id,
        opportunity=opp,
        contact_id=draft.recipient_contact_id,
        channel="whatsapp",
        provider="whatsapp_manual",
        mailbox=None,
    )
    db.add(
        Message(
            workspace_id=workspace_id,
            conversation_id=conv.id,
            company_id=opp.company_id,
            direction="outbound",
            channel="whatsapp",
            subject="",
            body_text=draft.body,
            to_address=draft.recipient_snapshot.get("value"),
            link_status="linked",
        )
    )
    draft.status = "sent"
    await db.execute(
        update(Approval)
        .where(Approval.draft_id == draft.id, Approval.status == "active")
        .values(status="consumed")
    )
    opp.status = "contacted" if opp.status in ("qualified", "draft_ready") else opp.status
    opp.last_contacted_at = datetime.now(UTC)
    await db.flush()
    audit.record(
        db,
        workspace_id=workspace_id,
        actor_id=actor_id,
        action="manual_contact.whatsapp",
        entity_type="draft",
        entity_id=draft.id,
    )
    return mc


async def resolve_unknown_delivery(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    actor_id: str,
    command_id: uuid.UUID,
    outcome: Literal["sent", "not_sent"],
) -> OutboundCommand:
    """مصالحة بشرية لتسليم غير معروف: لا إعادة إرسال عمياء؛ «لم تُرسل» يعيد المسودة لاعتماد جديد."""
    cmd = (
        await db.execute(
            select(OutboundCommand)
            .where(OutboundCommand.id == command_id, OutboundCommand.workspace_id == workspace_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cmd is None:
        raise NotFound()
    if cmd.status != "unknown_delivery":
        raise Conflict("الأمر ليس في حالة تسليم غير معروف", code="not_unknown")
    draft = await _get_draft(db, workspace_id, cmd.draft_id, lock=True)
    cmd.resolved_by = actor_id
    cmd.reconciliation_needed = False
    if outcome == "sent":
        from app.services.outbound import mark_sent

        await mark_sent(db, cmd, draft, provider_message_id=None, sent_copy_saved=False)
    else:
        cmd.status = "canceled"
        cmd.last_error = "أكد المراجع أنها لم تُرسل"
        draft.status = "pending_review"
        await db.execute(
            update(Approval)
            .where(
                Approval.draft_id == draft.id,
                Approval.status.in_(("active", "consumed")),
                Approval.decision == "approve",
            )
            .values(status="superseded")
        )
    audit.record(
        db,
        workspace_id=workspace_id,
        actor_id=actor_id,
        action=f"outbound.reconcile.{outcome}",
        entity_type="outbound_command",
        entity_id=cmd.id,
    )
    return cmd


async def expire_approvals(db: AsyncSession) -> int:
    now = datetime.now(UTC)
    rows = list(
        (
            await db.execute(
                select(Approval)
                .where(Approval.status == "active", Approval.expires_at < now)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    for a in rows:
        a.status = "expired"
        await db.execute(
            update(OutboundCommand)
            .where(OutboundCommand.approval_id == a.id, OutboundCommand.status == "pending")
            .values(status="canceled", last_error="انتهت صلاحية الاعتماد")
        )
        await db.execute(
            update(Draft)
            .where(Draft.id == a.draft_id, Draft.status == "approved")
            .values(status="pending_review")
        )
    return len(rows)
