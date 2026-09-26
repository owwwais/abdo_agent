"""تنفيذ أوامر الإرسال المعتمدة (SPEC §8.3–8.4، §11.1).

قبل الإرسال يُعاد فحص: صلاحية الاعتماد وعضوية المعتمد، أهلية التواصل ومنع التواصل، الإيقاف ومفتاح
OUTBOUND_ENABLED، حالة المنتج وإصداره، وردود أحدث من المسودة. الأمر يمر بـ pending → sending → sent /
unknown_delivery / failed / canceled. أمر وُجد في «sending» عند بدء مهمة يعني انقطاعًا سابقًا: يصبح
unknown_delivery ولا يُعاد إرساله آليًا.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import data_hash
from app.connectors.mail import OutgoingMail
from app.db.models import Company, Membership, Product, Suppression, Workspace
from app.db.models_sales import Approval, Draft, Message, Opportunity, OutboundCommand
from app.jobs import queue
from app.jobs.queue import Lease
from app.services import audit
from app.services import integrations as integ
from app.services.approvals import eligibility, ensure_conversation
from app.services.channels import ChannelNotReady, mail_context
from app.services.normalize import email_domain
from app.workflows.common import Deps


@dataclass
class SendResult:
    status: str
    detail: str


async def mark_sent(
    db: AsyncSession,
    cmd: OutboundCommand,
    draft: Draft,
    *,
    provider_message_id: str | None,
    sent_copy_saved: bool,
) -> None:
    now = datetime.now(UTC)
    cmd.status = "sent"
    cmd.sent_at = now
    cmd.last_error = None if sent_copy_saved else cmd.last_error
    draft.status = "sent"
    await db.execute(
        update(Approval).where(Approval.id == cmd.approval_id).values(status="consumed")
    )
    opp = await db.get(Opportunity, draft.opportunity_id)
    assert opp is not None
    if opp.status in ("qualified", "draft_ready", "discovered"):
        opp.status = "contacted"
    opp.last_contacted_at = now
    conv = await ensure_conversation(
        db,
        workspace_id=draft.workspace_id,
        opportunity=opp,
        contact_id=draft.recipient_contact_id,
        channel="email",
        provider=cmd.provider or "smtp",
        mailbox=None,
    )
    if draft.conversation_id is None:
        draft.conversation_id = conv.id
    in_reply_to = None
    if draft.reply_to_message_id:
        src = await db.get(Message, draft.reply_to_message_id)
        in_reply_to = src.internet_message_id if src else None
    db.add(
        Message(
            workspace_id=draft.workspace_id,
            conversation_id=conv.id,
            company_id=opp.company_id,
            direction="outbound",
            channel="email",
            provider_message_id=provider_message_id,
            internet_message_id=cmd.internet_message_id,
            in_reply_to=in_reply_to,
            to_address=draft.recipient_snapshot.get("value"),
            subject=draft.subject,
            body_text=draft.body,
            link_status="linked",
        )
    )
    await db.flush()


async def _blockers(
    db: AsyncSession, deps: Deps, cmd: OutboundCommand, draft: Draft, approval: Approval | None
) -> tuple[str | None, str]:
    """يعيد (سبب المنع، حالة المسودة الجديدة) أو (None, "")."""
    now = datetime.now(UTC)
    if approval is None or approval.status != "active" or approval.decision != "approve":
        return "الاعتماد لم يعد فعالًا", "pending_review"
    if approval.expires_at and approval.expires_at < now:
        return "انتهت صلاحية الاعتماد", "pending_review"
    if (
        approval.draft_revision != draft.revision
        or approval.content_hash != draft.content_hash
        or draft.status != "approved"
    ):
        return "المسودة تغيرت بعد الاعتماد", "pending_review"
    actor = approval.actor_id.removeprefix("user:")
    try:
        actor_uuid = uuid.UUID(actor)
    except ValueError:
        return "هوية المعتمد غير صالحة", "pending_review"
    member = (
        await db.execute(
            select(Membership).where(
                Membership.workspace_id == draft.workspace_id,
                Membership.auth_user_id == actor_uuid,
                Membership.status == "active",
            )
        )
    ).scalar_one_or_none()
    if member is None:
        return "عضوية المعتمد لم تعد فعالة", "pending_review"
    opp = await db.get(Opportunity, draft.opportunity_id)
    product = await db.get(Product, opp.product_id) if opp else None
    if product is None or product.status != "active" or product.version != draft.product_version:
        return "المنتج أوقف أو تغير منذ الاعتماد", "stale"
    if await eligibility(db, draft.recipient_contact_id) != "allowed":
        return "أهلية التواصل لم تعد مؤكدة", "pending_review"
    email = str(draft.recipient_snapshot.get("value", "")).lower()
    hashes = [("email", data_hash(deps.settings, "email", email))]
    dom = email_domain(email) if "@" in email else None
    if dom:
        hashes.append(("domain", data_hash(deps.settings, "domain", dom)))
    company = await db.get(Company, opp.company_id) if opp else None
    if company is not None:
        hashes.append(("company", data_hash(deps.settings, "company", str(company.id))))
        if company.domain:
            hashes.append(("domain", data_hash(deps.settings, "domain", company.domain)))
    for scope, h in hashes:
        hit = (
            await db.execute(
                select(func.count())
                .select_from(Suppression)
                .where(
                    Suppression.workspace_id == draft.workspace_id,
                    Suppression.scope == scope,
                    Suppression.target_hash == h,
                )
            )
        ).scalar_one()
        if hit:
            return "المستلم أو الجهة في سجل منع التواصل", "canceled"
    if draft.conversation_id is not None:
        newer = (
            await db.execute(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.conversation_id == draft.conversation_id,
                    Message.direction == "inbound",
                    Message.created_at > draft.created_at,
                )
            )
        ).scalar_one()
        if newer:
            return "وصل رد أحدث بعد كتابة المسودة؛ تحتاج مراجعة", "stale"
    allow = [a.strip().lower() for a in deps.settings.outbound_allowlist.split(",") if a.strip()]
    if allow and not any(email == a or (a.startswith("@") and email.endswith(a)) for a in allow):
        return "المستلم خارج قائمة السماح OUTBOUND_ALLOWLIST", "pending_review"
    return None, ""


async def send_outbound(deps: Deps, lease: Lease) -> dict[str, str]:
    ws = lease.workspace_id
    assert ws is not None
    draft_id = uuid.UUID(lease.payload["draft_id"])
    revision = int(lease.payload["revision"])
    # المرحلة 1: التحقق ووضع «sending» في معاملة مسيجة بالحجز.
    async with deps.sm() as db, db.begin():
        await queue.assert_lease(db, lease)
        cmd = (
            await db.execute(
                select(OutboundCommand)
                .where(
                    OutboundCommand.draft_id == draft_id, OutboundCommand.draft_revision == revision
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if cmd is None or cmd.status in ("sent", "canceled", "failed", "unknown_delivery"):
            await queue.complete(db, lease, {"status": cmd.status if cmd else "missing"})
            return {"status": cmd.status if cmd else "missing"}
        if cmd.status == "sending":
            cmd.status = "unknown_delivery"
            cmd.reconciliation_needed = True
            cmd.last_error = "انقطع العامل أثناء الإرسال؛ النتيجة غير معروفة"
            await queue.complete(db, lease, {"status": "unknown_delivery"})
            return {"status": "unknown_delivery"}
        draft = (
            await db.execute(select(Draft).where(Draft.id == draft_id).with_for_update())
        ).scalar_one()
        approval = await db.get(Approval, cmd.approval_id)
        reason, draft_status = await _blockers(db, deps, cmd, draft, approval)
        if reason:
            cmd.status = "canceled"
            cmd.last_error = reason
            draft.status = draft_status
            if approval is not None and approval.status == "active":
                approval.status = "superseded"
            audit.record(
                db,
                workspace_id=ws,
                actor_id="service:outbound",
                action="outbound.blocked",
                entity_type="outbound_command",
                entity_id=cmd.id,
                change={"reason": reason},
            )
            await queue.complete(db, lease, {"status": "canceled", "reason": reason})
            return {"status": "canceled", "reason": reason}
        ops = await integ.get_config(db, deps.settings, ws, "operations", integ.OperationsConfig)
        workspace = await db.get(Workspace, ws)
        tz = ZoneInfo(workspace.timezone if workspace else deps.settings.app_timezone)
        try:
            mctx = await mail_context(db, deps.settings, ws)
        except ChannelNotReady as exc:
            cmd.last_error = str(exc)
            await queue.reschedule(db, lease, datetime.now(UTC) + timedelta(hours=1), str(exc))
            return {"status": "pending", "reason": str(exc)}
        if ops.pause_outbound:
            await queue.reschedule(
                db, lease, datetime.now(UTC) + timedelta(minutes=30), "الإرسال موقوف من الإعدادات"
            )
            return {"status": "pending", "reason": "paused"}
        if mctx.real and not deps.settings.outbound_enabled:
            await queue.reschedule(
                db, lease, datetime.now(UTC) + timedelta(hours=1), "OUTBOUND_ENABLED=false"
            )
            return {"status": "pending", "reason": "outbound_disabled"}
        day_start = datetime.combine(datetime.now(tz).date(), time.min, tzinfo=tz).astimezone(UTC)
        sent_today = (
            await db.execute(
                select(func.count())
                .select_from(OutboundCommand)
                .where(
                    OutboundCommand.workspace_id == ws,
                    OutboundCommand.status == "sent",
                    OutboundCommand.sent_at >= day_start,
                )
            )
        ).scalar_one()
        if sent_today >= mctx.config.daily_send_limit:
            await queue.reschedule(
                db, lease, day_start + timedelta(days=1, hours=9), "بلغ حد الإرسال اليومي"
            )
            return {"status": "pending", "reason": "daily_limit"}
        in_reply_to, references = None, ""
        reply_uid: str | None = None
        if draft.reply_to_message_id:
            src = await db.get(Message, draft.reply_to_message_id)
            if src is not None:
                in_reply_to, references = src.internet_message_id, src.references
                reply_uid = src.provider_message_id
        mail = OutgoingMail(
            from_address=mctx.config.from_address,
            from_name=mctx.config.from_name,
            to_address=str(draft.recipient_snapshot.get("value", "")),
            subject=draft.subject,
            body=draft.body,
            reply_to=mctx.config.reply_to,
            in_reply_to=in_reply_to,
            references=references,
            reply_uid=reply_uid if reply_uid and reply_uid.isdigit() else None,
            reply_folder=mctx.config.imap_folder if reply_uid else None,
        )
        cmd.status = "sending"
        cmd.attempts += 1
        cmd.provider = mctx.mailbox.name
    # المرحلة 2: الإرسال خارج أي معاملة.
    outcome = await mctx.mailbox.send(mail)
    # المرحلة 3: تسجيل النتيجة.
    async with deps.sm() as db, db.begin():
        cmd = (
            await db.execute(
                select(OutboundCommand).where(OutboundCommand.id == cmd.id).with_for_update()
            )
        ).scalar_one()
        draft = (
            await db.execute(select(Draft).where(Draft.id == draft_id).with_for_update())
        ).scalar_one()
        cmd.internet_message_id = outcome.message_id
        if outcome.status == "sent":
            await mark_sent(
                db, cmd, draft, provider_message_id=None, sent_copy_saved=outcome.sent_copy_saved
            )
        elif outcome.status == "unknown_delivery":
            cmd.status = "unknown_delivery"
            cmd.reconciliation_needed = True
            cmd.last_error = outcome.detail
        else:
            cmd.status = "pending" if outcome.retryable and cmd.attempts < 3 else "failed"
            cmd.last_error = outcome.detail
        audit.record(
            db,
            workspace_id=ws,
            actor_id="service:outbound",
            action=f"outbound.{cmd.status}",
            entity_type="outbound_command",
            entity_id=cmd.id,
            change={"detail": outcome.detail[:200]},
        )
        try:
            if cmd.status == "pending":
                await queue.reschedule(
                    db,
                    lease,
                    datetime.now(UTC) + timedelta(minutes=5 * cmd.attempts),
                    outcome.detail,
                )
            else:
                await queue.complete(db, lease, {"status": cmd.status})
        except queue.LeaseLost:
            # الإرسال حدث فعلًا؛ نسجل النتيجة حتى لو فقد العامل حجزه كي لا يُعاد الإرسال.
            pass
    return {"status": cmd.status, "detail": outcome.detail}
