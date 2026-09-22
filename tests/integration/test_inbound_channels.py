"""M4: الردود الواردة والويب هوك وتيليجرام (F-010، F-011، T14–T16)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.connectors.mail import FakeMailbox
from app.db.models import Contact, Job, Suppression, UserProfile
from app.db.models_sales import (
    Approval,
    Draft,
    InboundEvent,
    Message,
    Opportunity,
    OutboundCommand,
    TelegramCallback,
    TelegramLinkCode,
)
from app.services import approvals
from app.services import integrations as integ
from app.services.channels import TEST_HOOKS, telegram_context
from app.services.inbound import draft_reply, link_message, process_inbound, sync_mailbox
from app.services.secrets import set_secret
from app.services.telegram_bot import handle_update, send_draft_card
from app.workflows.common import Deps
from tests.conftest import ClientFactory, WorkspaceFixture, make_settings
from tests.sales_fixtures import approve, inbound, run_send, seed_draft

CHAT = "-1001234567890"


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    FakeMailbox.reset()
    yield
    FakeMailbox.reset()
    TEST_HOOKS["telegram_transport"] = None


def deps(sm: async_sessionmaker[AsyncSession]) -> Deps:
    return Deps(settings=make_settings(), sm=sm)


async def count(sm: async_sessionmaker[AsyncSession], model: type, *where: object) -> int:
    async with sm() as db:
        return int(
            (await db.execute(select(func.count()).select_from(model).where(*where))).scalar_one()
        )


async def sent_conversation(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> tuple[Any, str]:
    s = await seed_draft(sm, ws)
    await approve(sm, ws, s.draft)
    result = await run_send(sm, deps(sm))
    assert result is not None and result["status"] == "sent"
    return s, str(FakeMailbox.outbox[0]["Message-ID"])


async def test_reply_is_linked_classified_and_answered_once(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    """T14: الرد يُربط بالمحادثة عبر In-Reply-To، ولا يتكرر بتكرار المزامنة."""
    s, sent_id = await sent_conversation(sm, ws)
    FakeMailbox.inbox.append(
        inbound(1, body="شكرًا، هل يمكن ترتيب اتصال الأسبوع القادم؟", in_reply_to=sent_id)
    )
    first = await sync_mailbox(deps(sm), ws.id)
    assert first["fetched"] == 1 and first.get("linked") == 1, first
    second = await sync_mailbox(deps(sm), ws.id)
    assert second["fetched"] == 0  # المؤشر تقدّم
    async with sm() as db:
        msg = (await db.execute(select(Message).where(Message.direction == "inbound"))).scalar_one()
        opp = await db.get(Opportunity, s.opportunity)
    assert msg.link_status == "linked" and msg.classification == "meeting_request"
    assert opp is not None and opp.status == "meeting_proposed"
    # نفس الرسالة مرة أخرى (إعادة تسليم من الخادم) → مكررة
    dup = await process_inbound(
        deps(sm),
        ws.id,
        inbound(2, body="نسخة", in_reply_to=sent_id, message_id=msg.internet_message_id),
    )
    assert dup["status"] == "duplicate"
    assert await count(sm, Job, Job.kind == "process_reply") == 1
    reply = await draft_reply(deps(sm), ws.id, msg.id)
    assert reply["status"] == "draft_ready", reply
    async with sm() as db:
        d = (await db.execute(select(Draft).where(Draft.kind == "reply"))).scalar_one()
    assert (
        d.status == "pending_review"
        and d.subject.startswith("رد")
        and d.reply_to_message_id == msg.id
    )


async def test_auto_reply_does_not_trigger_a_draft(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    _, sent_id = await sent_conversation(sm, ws)
    r = await process_inbound(
        deps(sm),
        ws.id,
        inbound(1, body="أنا خارج المكتب حتى الأحد", in_reply_to=sent_id, auto=True),
    )
    assert r["classification"] == "auto_reply"
    assert await count(sm, Job, Job.kind == "process_reply") == 0


async def test_ambiguous_sender_needs_manual_linking(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    """T15: رسالة من جهة معروفة بلا مرجع رسالة ولا محادثة مفتوحة → needs_linking ثم ربط يدوي موثق."""
    s = await seed_draft(sm, ws)
    r = await process_inbound(deps(sm), ws.id, inbound(1, body="مرحبًا، أرسلوا لنا تفاصيل أكثر."))
    assert r["status"] == "needs_linking"
    async with sm() as db:
        msg = (await db.execute(select(Message))).scalar_one()
    assert msg.candidate_opportunity_ids == [str(s.opportunity)]
    # مرسل لا يخص أي جهة معروفة → يُتجاهل ولا يُخزن
    ignored = await process_inbound(
        deps(sm), ws.id, inbound(2, body="عرض", from_address="x@unrelated.example")
    )
    assert ignored["status"] == "ignored"
    async with sm() as db, db.begin():
        await link_message(
            db,
            workspace_id=ws.id,
            actor_id="user:x",
            message_id=msg.id,
            opportunity_id=s.opportunity,
        )
    async with sm() as db:
        msg = await db.get(Message, msg.id)
    assert msg is not None and msg.link_status == "linked" and msg.conversation_id is not None


async def test_unsubscribe_suppresses_and_cancels_everything(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    """T16: طلب الإيقاف يسجل منع التواصل ويلغي المسودات والأوامر المعلقة للجهة فورًا."""
    s, sent_id = await sent_conversation(sm, ws)
    async with sm() as db, db.begin():
        opp = await db.get(Opportunity, s.opportunity)
        assert opp is not None
        contact = (await db.execute(select(Contact))).scalars().first()
        await approvals.create_draft(
            db,
            workspace_id=ws.id,
            opportunity=opp,
            kind="followup",
            channel="email",
            contact=contact,
            subject="متابعة",
            body="متابعة قصيرة",
            product_version=1,
            findings=[],
            model_meta={},
            created_by="t",
        )
    r = await process_inbound(
        deps(sm), ws.id, inbound(1, body="الرجاء إيقاف التواصل وعدم مراسلتنا", in_reply_to=sent_id)
    )
    assert r["classification"] == "unsubscribe"
    assert await count(sm, Suppression) >= 2
    assert await count(sm, Draft, Draft.status == "pending_review") == 0
    async with sm() as db:
        opp = await db.get(Opportunity, s.opportunity)
    assert opp is not None and opp.status == "not_interested"


async def test_hostinger_webhook_requires_secret_and_dedupes(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture, client_for: ClientFactory
) -> None:
    async with sm() as db, db.begin():
        await set_secret(
            db, make_settings(), ws.id, "hostinger_webhook_secret", "hook-secret-for-tests", "t"
        )
    client = await client_for()
    body = json.dumps({"event": "message.received", "id": "evt-1"})
    assert (await client.post("/webhooks/email/hostinger", content=body)).status_code == 401
    bad = await client.post(
        "/webhooks/email/hostinger", content=body, headers={"Authorization": "Bearer wrong"}
    )
    assert bad.status_code == 401
    ok = await client.post(
        "/webhooks/email/hostinger",
        content=body,
        headers={"Authorization": "Bearer hook-secret-for-tests"},
    )
    assert ok.status_code == 200 and ok.json() == {"ok": True, "duplicate": False}
    again = await client.post(
        "/webhooks/email/hostinger",
        content=body,
        headers={"Authorization": "Bearer hook-secret-for-tests"},
    )
    assert again.json()["duplicate"] is True
    assert await count(sm, InboundEvent, InboundEvent.provider == "hostinger") == 1
    assert await count(sm, Job, Job.kind == "mail_sync") == 1


class FakeTelegram:
    """نقل httpx اصطناعي لواجهة Bot API يسجل الطلبات."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content or b"{}")
        self.calls.append((method, payload))
        result: Any = True
        if method == "sendMessage":
            result = {"message_id": 77, "chat": {"id": int(payload["chat_id"])}}
        return httpx.Response(200, json={"ok": True, "result": result})

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


async def enable_telegram(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> FakeTelegram:
    fake = FakeTelegram()
    TEST_HOOKS["telegram_transport"] = httpx.MockTransport(fake)
    async with sm() as db, db.begin():
        await set_secret(db, make_settings(), ws.id, "telegram_bot_token", "123456:TEST-token", "t")
        await integ.save_config(
            db,
            ws.id,
            "telegram",
            integ.TelegramConfig(enabled=True, chat_id=CHAT),
            version=0,
            actor_id="t",
        )
    return fake


async def test_telegram_card_link_and_approve(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    fake = await enable_telegram(sm, ws)
    s = await seed_draft(sm, ws)
    assert (await send_draft_card(deps(sm), ws.id, s.draft))["status"] == "sent"
    card = next(p for m, p in fake.calls if m == "sendMessage")
    assert "مجمع عيادات الواحة" in card["text"] and "123456:TEST-token" not in card["text"]
    async with sm() as db:
        approve_token = (
            (await db.execute(select(TelegramCallback).where(TelegramCallback.action == "approve")))
            .scalar_one()
            .token
        )
        tg = await telegram_context(db, make_settings(), ws.id)
    assert tg.client is not None

    def callback(update_id: int, user: int, chat: str = CHAT) -> dict[str, Any]:
        return {
            "update_id": update_id,
            "callback_query": {
                "id": f"cq{update_id}",
                "from": {"id": user},
                "data": approve_token,
                "message": {"chat": {"id": int(chat)}, "message_id": 77},
            },
        }

    # مجموعة غير مصرح بها، ثم مستخدم غير مربوط
    assert (await handle_update(deps(sm), ws.id, callback(1, 555, "-100999"), tg.client, CHAT))[
        "status"
    ] == "forbidden_chat"
    assert (await handle_update(deps(sm), ws.id, callback(2, 555), tg.client, CHAT))[
        "status"
    ] == "forbidden_user"
    # ربط الحساب برمز لمرة واحدة
    async with sm() as db, db.begin():
        db.add(
            TelegramLinkCode(
                code="LINK1234",
                workspace_id=ws.id,
                auth_user_id=ws.reviewer.auth_user_id,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
    link = {
        "update_id": 3,
        "message": {"text": "/link LINK1234", "from": {"id": 555}, "chat": {"id": 555}},
    }
    await handle_update(deps(sm), ws.id, link, tg.client, CHAT)
    async with sm() as db:
        profile = await db.get(UserProfile, ws.reviewer.auth_user_id)
    assert profile is not None and profile.telegram_user_id == 555
    decided = await handle_update(deps(sm), ws.id, callback(4, 555), tg.client, CHAT)
    assert decided == {"status": "decided", "decision": "approve", "created": True}
    # تكرار التحديث نفسه من تيليجرام، ثم ضغطة ثانية على الزر
    assert (await handle_update(deps(sm), ws.id, callback(4, 555), tg.client, CHAT))[
        "status"
    ] == "duplicate"
    again = await handle_update(deps(sm), ws.id, callback(5, 555), tg.client, CHAT)
    assert again["created"] is False
    assert await count(sm, Approval) == 1 and await count(sm, OutboundCommand) == 1
    async with sm() as db:
        approval = (await db.execute(select(Approval))).scalar_one()
    assert approval.via == "telegram" and approval.actor_id == f"user:{ws.reviewer.auth_user_id}"
    assert "editMessageReplyMarkup" in fake.methods()


async def test_telegram_webhook_checks_secret_header(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture, client_for: ClientFactory
) -> None:
    await enable_telegram(sm, ws)
    async with sm() as db, db.begin():
        await set_secret(
            db, make_settings(), ws.id, "telegram_webhook_secret", "tg-hook-secret", "t"
        )
    client = await client_for()
    update = {"update_id": 9, "message": {"text": "مرحبا", "from": {"id": 1}, "chat": {"id": 1}}}
    assert (await client.post("/webhooks/telegram", json=update)).status_code == 401
    ok = await client.post(
        "/webhooks/telegram",
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-hook-secret"},
    )
    assert ok.status_code == 200
    assert await count(sm, InboundEvent, InboundEvent.provider == "telegram") == 1
