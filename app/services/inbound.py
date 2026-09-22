"""البريد الوارد (SPEC §8.5): مزامنة بمؤشر محفوظ، منع التكرار بمعرفات الرسائل، الربط الحذر، التصنيف،
واحترام طلب إيقاف التواصل فورًا دون انتظار موافقة.

ترتيب الربط: In-Reply-To/References لرسالة صادرة منا ← عنوان المرسل لجهة اتصال معروفة (محادثة واحدة
مفتوحة) ← غموض → needs_linking. الرسائل غير المرتبطة بأي جهة معروفة لا تُخزن (تقليل البيانات).
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.gateway import CallCounter, CallLimitReached, ModelNotReady, ModelOutputInvalid
from app.agents.prompts import (
    CLASSIFY_SYSTEM,
    PROMPT_VERSION,
    REPLY_SYSTEM,
    ReplyClass,
    ReplyDraftOut,
    context_block,
)
from app.agents.providers import ProviderError
from app.auth.security import data_hash
from app.connectors.mail import InboundMail
from app.db.models import Company, Contact, Product, Suppression
from app.db.models_sales import (
    Conversation,
    Draft,
    InboundEvent,
    Message,
    Opportunity,
    OutboundCommand,
)
from app.jobs import queue
from app.services import approvals, audit, budget
from app.services import integrations as integ
from app.services.channels import ChannelNotReady, mail_context
from app.services.normalize import email_domain
from app.services.policy import check_draft
from app.workflows.common import Deps

_UNSUB = re.compile(
    r"(إيقاف|ايقاف|أوقفوا|اوقفوا|توقفوا|لا ترسلوا|لا تراسلونا|لا تتواصلوا|احذفوا|إلغاء الاشتراك|الغاء الاشتراك|"
    r"unsubscribe|remove me|stop (emailing|contacting)|do not contact)",
    re.I,
)


def _event_id(provider: str, raw: bytes) -> str:
    return hashlib.sha256(provider.encode() + b":" + raw).hexdigest()[:64]


async def record_webhook_event(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID | None,
    provider: str,
    body: bytes,
    payload: dict[str, Any],
) -> bool:
    """يخزن الحدث قبل المعالجة؛ يعيد False إن كان مكررًا (تكرار الويب هوك لا يكرر التحليل)."""
    event_id = str(payload.get("id") or payload.get("event_id") or _event_id(provider, body))[:200]
    brief = {k: payload[k] for k in ("event", "type", "mailbox", "timestamp", "id") if k in payload}
    res = await db.execute(
        insert(InboundEvent)
        .values(
            workspace_id=workspace_id,
            provider=provider,
            event_id=event_id,
            payload=brief,
            status="received",
        )
        .on_conflict_do_nothing()
        .returning(InboundEvent.id)
    )
    return res.scalar_one_or_none() is not None


async def suppress_contact(
    db: AsyncSession,
    deps: Deps,
    *,
    workspace_id: uuid.UUID,
    email: str,
    company_id: uuid.UUID | None,
    reason: str,
) -> int:
    """إيقاف فوري: بصمة البريد والجهة، وإلغاء كل المسودات وأوامر الإرسال المعلقة للجهة."""
    targets = [("email", data_hash(deps.settings, "email", email.lower()))]
    if company_id:
        targets.append(("company", data_hash(deps.settings, "company", str(company_id))))
    for scope, h in targets:
        await db.execute(
            insert(Suppression)
            .values(
                workspace_id=workspace_id,
                scope=scope,
                target_hash=h,
                reason=reason[:200],
                created_by="service:inbound",
            )
            .on_conflict_do_nothing()
        )
    canceled = 0
    if company_id:
        opp_ids = list(
            (
                await db.execute(select(Opportunity.id).where(Opportunity.company_id == company_id))
            ).scalars()
        )
        if opp_ids:
            drafts = list(
                (
                    await db.execute(
                        select(Draft).where(
                            Draft.opportunity_id.in_(opp_ids),
                            Draft.status.in_(("pending_review", "approved", "deferred")),
                        )
                    )
                ).scalars()
            )
            for d in drafts:
                d.status = "canceled"
                canceled += 1
            await db.execute(
                update(OutboundCommand)
                .where(
                    OutboundCommand.draft_id.in_([d.id for d in drafts] or [uuid.UUID(int=0)]),
                    OutboundCommand.status == "pending",
                )
                .values(status="canceled", last_error="طلب إيقاف التواصل")
            )
            await db.execute(
                update(Opportunity)
                .where(
                    Opportunity.id.in_(opp_ids),
                    Opportunity.status.notin_(("won", "lost", "archived")),
                )
                .values(status="not_interested", disqualify_reason="طلب إيقاف التواصل")
            )
    audit.record(
        db,
        workspace_id=workspace_id,
        actor_id="service:inbound",
        action="suppression.opt_out",
        entity_type="company",
        entity_id=company_id,
        change={"canceled_drafts": canceled},
    )
    return canceled


async def _link(
    db: AsyncSession, deps: Deps, workspace_id: uuid.UUID, mail: InboundMail
) -> tuple[Conversation | None, str, list[str]]:
    refs = [r for r in re.findall(r"<[^>]+>", f"{mail.in_reply_to or ''} {mail.references}")]
    if refs:
        conv = (
            await db.execute(
                select(Conversation)
                .join(Message, Message.conversation_id == Conversation.id)
                .where(
                    Message.workspace_id == workspace_id,
                    Message.direction == "outbound",
                    Message.internet_message_id.in_(refs),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if conv is not None:
            return conv, "linked", []
    h = data_hash(deps.settings, "email", mail.from_address.lower())
    company_ids = set(
        (
            await db.execute(
                select(Contact.company_id).where(
                    Contact.workspace_id == workspace_id,
                    Contact.channel == "email",
                    Contact.value_hash == h,
                )
            )
        ).scalars()
    )
    if not company_ids:
        dom = email_domain(mail.from_address)
        if dom:
            company_ids = set(
                (
                    await db.execute(
                        select(Company.id).where(
                            Company.workspace_id == workspace_id, Company.domain == dom
                        )
                    )
                ).scalars()
            )
    company_ids.discard(None)
    if not company_ids:
        return None, "ignored", []
    convs = list(
        (
            await db.execute(
                select(Conversation).where(
                    Conversation.workspace_id == workspace_id,
                    Conversation.company_id.in_(company_ids),
                    Conversation.channel == "email",
                    Conversation.status == "open",
                )
            )
        ).scalars()
    )
    if len(convs) == 1:
        return convs[0], "linked", []
    candidates = [str(c.opportunity_id) for c in convs if c.opportunity_id]
    if not candidates:
        # الجهة معروفة بلا محادثة مفتوحة: فرصها مرشحة للربط اليدوي.
        candidates = [
            str(i)
            for i in (
                await db.execute(
                    select(Opportunity.id).where(
                        Opportunity.workspace_id == workspace_id,
                        Opportunity.company_id.in_(company_ids),
                    )
                )
            ).scalars()
        ]
    return None, "needs_linking", candidates


async def classify(
    deps: Deps, workspace_id: uuid.UUID, mail: InboundMail, context: dict[str, Any]
) -> tuple[str, str, bool]:
    if mail.is_bounce:
        return "bounce", "رسالة ارتداد", False
    if mail.auto_submitted:
        return "auto_reply", "رد آلي", False
    if _UNSUB.search(f"{mail.subject}\n{mail.body_text}"):
        return "unsubscribe", "طلب إيقاف التواصل", False
    try:
        out, _ = await deps.gateway(workspace_id).complete(
            role="extractor",
            system=CLASSIFY_SYSTEM,
            user="صنّف هذا الرد.\n"
            + context_block({**context, "subject": mail.subject, "message": mail.body_text[:4000]}),
            output_model=ReplyClass,
            schema_name="reply_classification",
            category="followup",
            counter=CallCounter(2),
            prompt_version=PROMPT_VERSION,
        )
        return out.classification, out.summary, out.needs_human
    except (
        ModelNotReady,
        budget.BudgetBlocked,
        ProviderError,
        ModelOutputInvalid,
        CallLimitReached,
    ) as exc:
        return "ambiguous", f"تعذر التصنيف الآلي: {exc}", True


async def process_inbound(deps: Deps, workspace_id: uuid.UUID, mail: InboundMail) -> dict[str, Any]:
    async with deps.sm() as db, db.begin():
        if mail.message_id:
            dup = (
                await db.execute(
                    select(func.count())
                    .select_from(Message)
                    .where(
                        Message.workspace_id == workspace_id,
                        Message.internet_message_id == mail.message_id,
                    )
                )
            ).scalar_one()
            if dup:
                return {"status": "duplicate"}
        conv, link_status, candidates = await _link(db, deps, workspace_id, mail)
        if link_status == "ignored":
            return {"status": "ignored"}
        company_id = conv.company_id if conv else None
        if company_id is None and candidates:
            opp = await db.get(Opportunity, uuid.UUID(candidates[0]))
            company_id = opp.company_id if opp else None
        opp = (
            await db.get(Opportunity, conv.opportunity_id) if conv and conv.opportunity_id else None
        )
        company = await db.get(Company, company_id) if company_id else None
    context = {"company": {"name": company.display_name if company else ""}}
    cls, summary, needs_human = await classify(deps, workspace_id, mail, context)
    async with deps.sm() as db, db.begin():
        msg = Message(
            workspace_id=workspace_id,
            conversation_id=conv.id if conv else None,
            company_id=company_id,
            direction="inbound",
            channel="email",
            provider_message_id=mail.uid,
            internet_message_id=mail.message_id,
            in_reply_to=mail.in_reply_to,
            references=mail.references[:4000],
            from_address=mail.from_address,
            to_address=mail.to_address,
            subject=mail.subject,
            body_text=mail.body_text[:20000],
            classification=cls,
            classification_detail={"summary": summary, "needs_human": needs_human},
            link_status=link_status,
            candidate_opportunity_ids=candidates,
            sent_or_received_at=mail.date,
        )
        db.add(msg)
        await db.flush()
        # رد جديد يجعل المسودات والأوامر المعلقة للمحادثة قديمة حتى المراجعة.
        if conv is not None:
            stale = list(
                (
                    await db.execute(
                        select(Draft).where(
                            Draft.conversation_id == conv.id,
                            Draft.status.in_(("pending_review", "approved", "deferred")),
                        )
                    )
                ).scalars()
            )
            for d in stale:
                d.status = "stale"
            if stale:
                await db.execute(
                    update(OutboundCommand)
                    .where(
                        OutboundCommand.draft_id.in_([d.id for d in stale]),
                        OutboundCommand.status == "pending",
                    )
                    .values(status="canceled", last_error="وصل رد أحدث")
                )
        if cls == "unsubscribe":
            await suppress_contact(
                db,
                deps,
                workspace_id=workspace_id,
                email=mail.from_address,
                company_id=company_id,
                reason="طلب إيقاف التواصل في رد بريد",
            )
        elif opp is not None and cls in ("interested", "question", "meeting_request", "not_fit"):
            # الفرصة حُمّلت في جلسة سابقة؛ نعيد تحميلها هنا كي يُحفظ التغيير فعلًا.
            opp = await db.get(Opportunity, opp.id, with_for_update=True)
            assert opp is not None
            opp.status = {
                "interested": "interested",
                "question": "interested",
                "meeting_request": "meeting_proposed",
                "not_fit": "not_interested",
            }[cls]
            if cls != "not_fit":
                await queue.enqueue(
                    db,
                    kind="process_reply",
                    workspace_id=workspace_id,
                    payload={"message_id": str(msg.id)},
                    idempotency_key=f"reply:{msg.id}",
                )
        audit.record(
            db,
            workspace_id=workspace_id,
            actor_id="service:inbound",
            action="message.inbound",
            entity_type="message",
            entity_id=msg.id,
            change={"classification": cls, "link": link_status},
        )
    return {"status": link_status, "classification": cls, "message_id": str(msg.id)}


async def sync_mailbox(deps: Deps, workspace_id: uuid.UUID) -> dict[str, Any]:
    async with deps.sm() as db:
        try:
            ctx = await mail_context(db, deps.settings, workspace_id)
        except ChannelNotReady as exc:
            return {"status": "not_configured", "reason": str(exc)}
        cursor = await integ.get_cursor(db, workspace_id, "mail")
    mails, new_cursor = await ctx.mailbox.fetch_new(cursor)
    results: dict[str, int] = {}
    for mail in mails:
        r = await process_inbound(deps, workspace_id, mail)
        results[r["status"]] = results.get(r["status"], 0) + 1
    async with deps.sm() as db, db.begin():
        await integ.set_cursor(db, workspace_id, "mail", new_cursor)
        await db.execute(
            update(InboundEvent)
            .where(InboundEvent.workspace_id == workspace_id, InboundEvent.status == "received")
            .values(status="processed", processed_at=datetime.now(UTC))
        )
    return {"status": "ok", "fetched": len(mails), **results}


async def draft_reply(deps: Deps, workspace_id: uuid.UUID, message_id: uuid.UUID) -> dict[str, Any]:
    """رد مقترح على رسالة واردة؛ يحتاج اعتمادًا كأي رسالة خارجية. الردود مستثناة من مهلة التواصل الأول."""
    async with deps.sm() as db:
        msg = await db.get(Message, message_id)
        if msg is None or msg.conversation_id is None:
            return {"status": "skipped"}
        conv = await db.get(Conversation, msg.conversation_id)
        opp = (
            await db.get(Opportunity, conv.opportunity_id) if conv and conv.opportunity_id else None
        )
        if opp is None:
            return {"status": "skipped"}
        product = await db.get(Product, opp.product_id)
        company = await db.get(Company, opp.company_id)
        mail_cfg = await integ.get_config(db, deps.settings, workspace_id, "mail", integ.MailConfig)
        ops = await integ.get_config(
            db, deps.settings, workspace_id, "operations", integ.OperationsConfig
        )
        contact = await db.get(Contact, conv.contact_id) if conv and conv.contact_id else None
        suppressed = (
            await db.execute(
                select(func.count())
                .select_from(Suppression)
                .where(
                    Suppression.workspace_id == workspace_id,
                    Suppression.target_hash
                    == data_hash(deps.settings, "company", str(opp.company_id)),
                )
            )
        ).scalar_one()
    if ops.pause_all_processing or suppressed or product is None or company is None:
        return {"status": "skipped"}
    ctx = {
        "product": {
            "name": product.name,
            "summary": product.summary,
            "capabilities": product.capabilities,
            "unavailable": product.unavailable_capabilities,
            "price": product.price_text if product.price_status == "approved" else "غير معتمد",
        },
        "company": {"name": company.display_name},
        "subject": msg.subject,
        "message": msg.body_text[:4000],
        "classification": msg.classification,
        "booking_link": mail_cfg.booking_link,
    }
    try:
        out, usage = await deps.gateway(workspace_id).complete(
            role="writer",
            system=REPLY_SYSTEM,
            user="اقترح ردًا.\n" + context_block(ctx),
            output_model=ReplyDraftOut,
            schema_name="reply_draft",
            category="followup",
            counter=CallCounter(ops.max_model_calls_per_opportunity),
            opportunity_id=opp.id,
            prompt_version=PROMPT_VERSION,
        )
    except (
        ModelNotReady,
        budget.BudgetBlocked,
        ProviderError,
        ModelOutputInvalid,
        CallLimitReached,
    ) as exc:
        return {"status": "blocked", "reason": str(exc)}
    findings = [
        f.as_dict()
        for f in check_draft(
            subject=out.subject,
            body=out.body,
            product=product,
            capabilities_used=out.capabilities_used,
            support_texts=[msg.body_text, company.display_name],
            allowed_urls=[product.product_url or "", product.demo_url or "", mail_cfg.booking_link],
        )
        if f.code != "length"
    ]
    tail = [x for x in (mail_cfg.signature.strip(),) if x]
    body = out.body.strip() + ("\n\n" + "\n\n".join(tail) if tail else "")
    subject = out.subject if out.subject.startswith(("رد", "Re")) else f"رد: {msg.subject}"[:150]
    async with deps.sm() as db, db.begin():
        opp = await db.get(Opportunity, opp.id)
        assert opp is not None
        draft = await approvals.create_draft(
            db,
            workspace_id=workspace_id,
            opportunity=opp,
            kind="reply",
            channel="email",
            contact=contact,
            subject=subject,
            body=body,
            product_version=product.version,
            findings=findings,
            created_by="service:writer",
            model_meta={
                "model": f"{usage.provider}/{usage.model}",
                "cost": str(usage.cost),
                "prompt_version": PROMPT_VERSION,
            },
            conversation_id=conv.id if conv else None,
            reply_to_message_id=msg.id,
        )
        await queue.enqueue(
            db,
            kind="notify_draft",
            workspace_id=workspace_id,
            payload={"draft_id": str(draft.id)},
            idempotency_key=f"notify:{draft.id}:1",
        )
    return {"status": "draft_ready", "draft_id": str(draft.id)}


async def link_message(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    actor_id: str,
    message_id: uuid.UUID,
    opportunity_id: uuid.UUID,
) -> Message:
    """ربط يدوي موثق لرسالة غامضة بفرصة محددة."""
    from app.api.errors import Conflict, NotFound

    msg = (
        await db.execute(
            select(Message)
            .where(Message.id == message_id, Message.workspace_id == workspace_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    opp = (
        await db.execute(
            select(Opportunity).where(
                Opportunity.id == opportunity_id, Opportunity.workspace_id == workspace_id
            )
        )
    ).scalar_one_or_none()
    if msg is None or opp is None:
        raise NotFound()
    if msg.link_status != "needs_linking":
        raise Conflict("الرسالة مربوطة مسبقًا", code="already_linked")
    conv = await approvals.ensure_conversation(
        db,
        workspace_id=workspace_id,
        opportunity=opp,
        contact_id=None,
        channel="email",
        provider="smtp",
        mailbox=None,
    )
    msg.conversation_id = conv.id
    msg.company_id = opp.company_id
    msg.link_status = "linked"
    audit.record(
        db,
        workspace_id=workspace_id,
        actor_id=actor_id,
        action="message.link",
        entity_type="message",
        entity_id=msg.id,
        change={"opportunity_id": str(opportunity_id)},
    )
    if msg.classification in ("interested", "question", "meeting_request"):
        await queue.enqueue(
            db,
            kind="process_reply",
            workspace_id=workspace_id,
            payload={"message_id": str(msg.id)},
            idempotency_key=f"reply:{msg.id}",
        )
    return msg
