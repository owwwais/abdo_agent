"""بوت تيليجرام (SPEC §8.2–8.3): بطاقات الاعتماد وأزرارها، ربط حسابات الأعضاء، ومعالجة التحديثات.

- هوية المعتمد = from.id المربوط بعضوية فعالة (لا username ولا نص في الرسالة).
- callback_data رمز opaque عشوائي قصير العمر يُحل من القاعدة؛ لا يحمل مستلمًا ولا نصًا يثق به الخادم.
- الاعتماد من البوت يمر بخدمة الاعتماد نفسها المستخدمة في اللوحة.
- تكرار update_id لا يعالج مرتين (inbound_events).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.api.errors import AppError
from app.connectors.telegram import TelegramClient, TelegramError
from app.db.models import Company, Membership, Product, UserProfile
from app.db.models_sales import Draft, InboundEvent, Opportunity, TelegramCallback, TelegramLinkCode
from app.services import approvals, audit
from app.services import integrations as integ
from app.services.channels import telegram_context
from app.workflows.common import Deps

ACTIONS = {"a": "approve", "r": "reject", "d": "defer"}


def _mask(value: str) -> str:
    if "@" in value:
        local, dom = value.split("@", 1)
        return f"{local[:2]}•••@{dom}"
    return value[:4] + "•••" + value[-2:] if len(value) > 6 else "•••"


async def draft_card(
    deps: Deps, workspace_id: uuid.UUID, draft_id: uuid.UUID
) -> tuple[str, Draft] | None:
    async with deps.sm() as db:
        draft = await db.get(Draft, draft_id)
        if draft is None or draft.workspace_id != workspace_id:
            return None
        opp = await db.get(Opportunity, draft.opportunity_id)
        company = await db.get(Company, opp.company_id) if opp else None
        product = await db.get(Product, opp.product_id) if opp else None
        elig = await approvals.eligibility(db, draft.recipient_contact_id)
    facts = "\n".join(f"• {f['claim'][:140]}" for f in (opp.facts if opp else [])[:3]) or "—"
    unknowns = "، ".join((opp.missing_info if opp else [])[:4]) or "—"
    blocking = [f["message"] for f in draft.policy_findings if f.get("blocking")]
    elig_ar = {
        "allowed": "مؤكدة",
        "disallowed": "ممنوعة",
        "unknown": "غير مؤكدة — أكدها من اللوحة",
    }[elig]
    text = (
        f"📝 مسودة {'رد' if draft.kind == 'reply' else 'تواصل أول'} (إصدار {draft.revision})\n"
        f"الجهة: {company.display_name if company else '—'} · {company.sector or '' if company else ''} {company.region or '' if company else ''}\n"
        f"المنتج: {product.name if product else '—'} · الدرجة: {opp.score if opp and opp.score is not None else '—'}\n"
        f"الأدلة:\n{facts}\nمجهول: {unknowns}\n"
        f"القناة: {'بريد' if draft.channel == 'email' else 'واتساب يدوي'} → {_mask(str(draft.recipient_snapshot.get('value', '')))}\n"
        f"أهلية التواصل: {elig_ar}\n"
        f"التكلفة التقديرية: {draft.model_meta.get('cost', '—')}\n\n"
        f"الموضوع: {draft.subject}\n\n{draft.body[:2000]}"
    )
    if blocking:
        text += "\n\n⚠️ مخالفات سياسة تمنع الاعتماد: " + "؛ ".join(blocking)
    return text, draft


async def send_draft_card(
    deps: Deps, workspace_id: uuid.UUID, draft_id: uuid.UUID
) -> dict[str, Any]:
    async with deps.sm() as db:
        tg = await telegram_context(db, deps.settings, workspace_id)
        ops = await integ.get_config(
            db, deps.settings, workspace_id, "operations", integ.OperationsConfig
        )
    if tg.client is None or not tg.config.chat_id:
        return {"status": "skipped", "reason": "تيليجرام غير مهيأ"}
    card = await draft_card(deps, workspace_id, draft_id)
    if card is None:
        return {"status": "skipped"}
    text, draft = card
    if draft.status not in ("pending_review", "deferred"):
        return {"status": "skipped", "reason": f"المسودة {draft.status}"}
    expires = datetime.now(UTC) + timedelta(hours=ops.approval_expiry_hours)
    tokens: dict[str, str] = {}
    async with deps.sm() as db, db.begin():
        for short, action in ACTIONS.items():
            token = short + secrets.token_urlsafe(18)
            db.add(
                TelegramCallback(
                    token=token,
                    workspace_id=workspace_id,
                    draft_id=draft.id,
                    draft_revision=draft.revision,
                    action=action,
                    expires_at=expires,
                )
            )
            tokens[action] = token
    base = deps.settings.app_base_url.rstrip("/")
    buttons = [
        [
            {"text": "✅ اعتماد", "callback_data": tokens["approve"]},
            {"text": "✖️ رفض", "callback_data": tokens["reject"]},
        ],
        [{"text": "⏸ تأجيل يومًا", "callback_data": tokens["defer"]}],
    ]
    if base.startswith("https://"):
        buttons[1].append(
            {"text": "✏️ تعديل في اللوحة", "url": f"{base}/approvals#draft-{draft.id}"}
        )
    try:
        result = await tg.client.send_message(tg.config.chat_id, text, buttons=buttons)
    except TelegramError as exc:
        return {"status": "failed", "reason": str(exc)}
    async with deps.sm() as db, db.begin():
        for token in tokens.values():
            cb = await db.get(TelegramCallback, token)
            if cb is not None:
                cb.chat_id = int(result.get("chat", {}).get("id", 0) or 0) or None
                cb.message_id = int(result.get("message_id", 0) or 0) or None
    return {"status": "sent"}


async def _member_for(
    db: Any, workspace_id: uuid.UUID, telegram_user_id: int
) -> UserProfile | None:
    profile = (
        await db.execute(
            select(UserProfile).where(UserProfile.telegram_user_id == telegram_user_id)
        )
    ).scalar_one_or_none()
    if profile is None:
        return None
    member = (
        await db.execute(
            select(Membership).where(
                Membership.workspace_id == workspace_id,
                Membership.auth_user_id == profile.auth_user_id,
                Membership.status == "active",
            )
        )
    ).scalar_one_or_none()
    return profile if member is not None else None


async def handle_update(
    deps: Deps,
    workspace_id: uuid.UUID,
    update: dict[str, Any],
    client: TelegramClient,
    allowed_chat: str,
) -> dict[str, Any]:
    update_id = update.get("update_id")
    if update_id is None:
        return {"status": "ignored"}
    async with deps.sm() as db, db.begin():
        res = await db.execute(
            insert(InboundEvent)
            .values(
                workspace_id=workspace_id,
                provider="telegram",
                event_id=f"{workspace_id}:{update_id}",
                payload={"kind": "callback" if "callback_query" in update else "message"},
                status="processed",
                processed_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing()
            .returning(InboundEvent.id)
        )
        if res.scalar_one_or_none() is None:
            return {"status": "duplicate"}
    if "message" in update:
        return await _handle_message(
            deps, workspace_id, update["message"], client, allowed_chat, int(update_id)
        )
    if "callback_query" in update:
        return await _handle_callback(
            deps, workspace_id, update["callback_query"], client, allowed_chat
        )
    return {"status": "ignored"}


async def _link_account(deps: Deps, workspace_id: uuid.UUID, code: str, sender: int) -> str:
    async with deps.sm() as db, db.begin():
        link = (
            await db.execute(
                select(TelegramLinkCode)
                .where(TelegramLinkCode.code == code, TelegramLinkCode.workspace_id == workspace_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if link is None or link.used_at is not None or link.expires_at < datetime.now(UTC):
            return "رمز الربط غير صالح أو منتهي."
        existing = (
            await db.execute(select(UserProfile).where(UserProfile.telegram_user_id == sender))
        ).scalar_one_or_none()
        if existing is not None and existing.auth_user_id != link.auth_user_id:
            existing.telegram_user_id = None
        profile = await db.get(UserProfile, link.auth_user_id)
        assert profile is not None
        profile.telegram_user_id = sender
        link.used_at = datetime.now(UTC)
        audit.record(
            db,
            workspace_id=workspace_id,
            actor_id=f"user:{profile.auth_user_id}",
            action="telegram.link",
            entity_type="user",
            change={},
        )
        return f"تم ربط حسابك: {profile.display_name}\nأرسل /help لترى ما أستطيع فعله."


def _command(text_: str) -> tuple[str, str]:
    """('/today', 'باقي النص') مع إزالة لاحقة ‎@اسم_البوت‎ التي يضيفها تيليجرام في المجموعات."""
    if not text_.startswith("/"):
        return "", text_
    head, _, rest = text_.partition(" ")
    return head.split("@", 1)[0].lower(), rest.strip()


async def _questions_today(deps: Deps, workspace_id: uuid.UUID, auth_user_id: uuid.UUID) -> int:
    from sqlalchemy import func

    from app.db.models import Job, Workspace

    async with deps.sm() as db:
        ws = await db.get(Workspace, workspace_id)
        tz = ZoneInfo(ws.timezone if ws else deps.settings.app_timezone)
        start = datetime.now(UTC).astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(Job)
                    .where(
                        Job.workspace_id == workspace_id,
                        Job.kind == "telegram_answer",
                        Job.created_at >= start,
                        Job.payload["auth_user_id"].astext == str(auth_user_id),
                    )
                )
            ).scalar_one()
        )


async def _handle_message(
    deps: Deps,
    workspace_id: uuid.UUID,
    message: dict[str, Any],
    client: TelegramClient,
    allowed_chat: str,
    update_id: int,
) -> dict[str, Any]:
    text_ = str(message.get("text", "")).strip()
    sender = message.get("from", {}).get("id")
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    if not text_ or sender is None or chat_id is None:
        return {"status": "ignored"}
    private = chat.get("type") == "private" or chat_id == sender
    if not private and str(chat_id) != allowed_chat:
        return {"status": "ignored"}  # مجموعة غير مصرح بها: صمت
    command, rest = _command(text_)
    reply_to = message.get("message_id")

    async def say(body: str) -> None:
        try:
            await client.send_message(chat_id, body, reply_to=reply_to)
        except TelegramError:
            pass

    if command == "/link":
        reply = await _link_account(
            deps, workspace_id, rest.split()[0] if rest else "", int(sender)
        )
        await say(reply)
        return {"status": "linked" if reply.startswith("تم") else "rejected"}

    async with deps.sm() as db:
        profile = await _member_for(db, workspace_id, int(sender))
        tg = await integ.get_config(
            db, deps.settings, workspace_id, "telegram", integ.TelegramConfig
        )
    if profile is None:
        if private:
            await say(
                "حسابك غير مربوط بعضوية فعالة. من اللوحة: الإعدادات ← الأعضاء ← إنشاء رمز ربط، ثم أرسل: /link الرمز"
            )
        return {"status": "forbidden_user"}

    from app.services import assistant

    if command in ("/start", "/help"):
        await say(assistant.HELP_TEXT)
        return {"status": "help"}
    if command == "/today":
        async with deps.sm() as db:
            facts = await assistant.today_facts(db, deps.settings, workspace_id)
        await say(assistant.today_text(facts))
        return {"status": "today"}
    if command == "/pending":
        async with deps.sm() as db:
            items = await assistant.pending_drafts(db, workspace_id)
        await say(assistant.pending_text(items, deps.settings.app_base_url))
        return {"status": "pending"}

    replied_to_bot = bool(message.get("reply_to_message", {}).get("from", {}).get("is_bot"))
    if command == "/ask":
        question = rest
    elif command:
        await say("أمر غير معروف. أرسل /help")
        return {"status": "unknown_command"}
    elif private or replied_to_bot:
        question = text_
    else:
        return {"status": "ignored"}  # كلام عادي في المجموعة لا يخص البوت
    if not question:
        await say("اكتب سؤالك بعد /ask، مثل: /ask ما حالة مجمع عيادات الواحة؟")
        return {"status": "empty_question"}
    if not tg.assistant_enabled:
        await say("الأسئلة الحرة معطلة من الإعدادات. الأوامر /today و/pending متاحة.")
        return {"status": "assistant_disabled"}
    if await _questions_today(deps, workspace_id, profile.auth_user_id) >= tg.assistant_daily_limit:
        await say(
            f"بلغت حد الأسئلة اليومي ({tg.assistant_daily_limit}). الأوامر /today و/pending متاحة."
        )
        return {"status": "limit"}
    from app.jobs import queue

    async with deps.sm() as db, db.begin():
        await queue.enqueue(
            db,
            kind="telegram_answer",
            workspace_id=workspace_id,
            payload={
                "chat_id": chat_id,
                "reply_to": reply_to,
                "question": question[:1000],
                "auth_user_id": str(profile.auth_user_id),
            },
            idempotency_key=f"tgq:{workspace_id}:{update_id}",
        )
    try:
        await client.send_chat_action(chat_id)
    except TelegramError:
        pass
    return {"status": "queued"}


async def answer_job(
    deps: Deps, workspace_id: uuid.UUID, payload: dict[str, Any]
) -> dict[str, Any]:
    """مهمة الطابور: يجيب النموذج ثم يرد في المحادثة نفسها. لا يكرر الرد إن أعيدت المهمة بعد نجاحها."""
    from app.services.assistant import answer_question

    async with deps.sm() as db:
        tg = await telegram_context(db, deps.settings, workspace_id)
    if tg.client is None:
        return {"status": "skipped", "reason": "تيليجرام غير مهيأ"}
    answer = await answer_question(deps, workspace_id, str(payload.get("question", "")))
    try:
        await tg.client.send_message(payload["chat_id"], answer, reply_to=payload.get("reply_to"))
    except TelegramError as exc:
        return {"status": "failed", "reason": str(exc)}
    return {"status": "answered"}


async def _handle_callback(
    deps: Deps,
    workspace_id: uuid.UUID,
    cq: dict[str, Any],
    client: TelegramClient,
    allowed_chat: str,
) -> dict[str, Any]:
    cq_id = str(cq.get("id", ""))
    chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
    sender = cq.get("from", {}).get("id")

    async def answer(msg: str) -> None:
        try:
            await client.answer_callback(cq_id, msg)
        except TelegramError:
            pass

    if allowed_chat and chat_id != allowed_chat:
        await answer("هذه المجموعة غير مصرح لها")
        return {"status": "forbidden_chat"}
    async with deps.sm() as db:
        profile = await _member_for(db, workspace_id, int(sender)) if sender is not None else None
        cb = await db.get(TelegramCallback, str(cq.get("data", "")))
    if profile is None:
        await answer("حسابك غير مربوط بعضوية فعالة؛ اربطه من الإعدادات")
        return {"status": "forbidden_user"}
    if cb is None or cb.workspace_id != workspace_id or cb.expires_at < datetime.now(UTC):
        await answer("انتهت صلاحية هذا الزر؛ راجع المسودة من اللوحة")
        return {"status": "expired"}
    async with deps.sm() as db:
        draft = await db.get(Draft, cb.draft_id)
    if draft is None:
        await answer("المسودة غير موجودة")
        return {"status": "missing"}
    try:
        async with deps.sm() as db, db.begin():
            approval, created = await approvals.decide(
                db,
                deps.settings,
                workspace_id=workspace_id,
                actor_id=f"user:{profile.auth_user_id}",
                auth_user_id=profile.auth_user_id,
                draft_id=cb.draft_id,
                revision=cb.draft_revision,
                content_hash=draft.content_hash if draft.revision == cb.draft_revision else "stale",
                decision=cast(Any, cb.action),
                via="telegram",
            )
            used = await db.get(TelegramCallback, cb.token)
            if used is not None:
                used.used_at = datetime.now(UTC)
    except AppError as exc:
        await answer(exc.message[:180])
        return {"status": "rejected", "code": exc.code}
    labels = {"approve": "اعتُمدت ✅", "reject": "رُفضت", "defer": "أُجلت يومًا"}
    await answer(
        labels.get(approval.decision, "سُجل القرار") + ("" if created else " (مسجلة مسبقًا)")
    )
    if cb.chat_id and cb.message_id:
        try:
            await client.edit_reply_markup(cb.chat_id, cb.message_id, None)
        except TelegramError:
            pass
    return {"status": "decided", "decision": approval.decision, "created": created}


async def poll_updates(deps: Deps, workspace_id: uuid.UUID) -> int:
    """وضع polling للتطوير المحلي: يسحب التحديثات من offset المحفوظ ويعالجها."""
    async with deps.sm() as db:
        tg = await telegram_context(db, deps.settings, workspace_id)
        cursor = await integ.get_cursor(db, workspace_id, "telegram")
    if tg.client is None or tg.config.mode != "polling":
        return 0
    offset = int(cursor.get("offset", 0))
    try:
        updates = await tg.client.get_updates(offset, long_poll=0)
    except TelegramError:
        return 0
    for upd in updates:
        await handle_update(deps, workspace_id, upd, tg.client, tg.config.chat_id)
        offset = max(offset, int(upd.get("update_id", 0)) + 1)
    if updates:
        async with deps.sm() as db, db.begin():
            await integ.set_cursor(db, workspace_id, "telegram", {"offset": offset})
    return len(updates)


async def send_text(deps: Deps, workspace_id: uuid.UUID, text_: str) -> bool:
    async with deps.sm() as db:
        tg = await telegram_context(db, deps.settings, workspace_id)
    if tg.client is None or not tg.config.chat_id:
        return False
    try:
        await tg.client.send_message(tg.config.chat_id, text_)
        return True
    except TelegramError:
        return False
