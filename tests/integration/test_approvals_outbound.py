"""M3: الاعتماد البشري والإرسال (F-008، F-009، T10–T13)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import Conflict
from app.connectors.mail import FakeMailbox
from app.db.models import Job, Product
from app.db.models_sales import (
    Approval,
    Draft,
    ManualContact,
    Message,
    Opportunity,
    OutboundCommand,
)
from app.jobs import queue
from app.services import approvals
from app.services import integrations as integ
from app.services.inbound import suppress_contact
from app.workflows.common import Deps
from tests.conftest import WorkspaceFixture, make_settings
from tests.sales_fixtures import approve, run_send, seed_draft


@pytest.fixture(autouse=True)
def _mailbox() -> Iterator[None]:
    FakeMailbox.reset()
    yield
    FakeMailbox.reset()


def deps(sm: async_sessionmaker[AsyncSession]) -> Deps:
    return Deps(settings=make_settings(), sm=sm)


async def count(sm: async_sessionmaker[AsyncSession], model: type, *where: object) -> int:
    async with sm() as db:
        return int(
            (await db.execute(select(func.count()).select_from(model).where(*where))).scalar_one()
        )


async def test_approval_requires_documented_eligibility(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    s = await seed_draft(sm, ws, allowed=False)
    with pytest.raises(Conflict) as exc:
        await approve(sm, ws, s.draft)
    assert exc.value.code == "eligibility_unknown"
    async with sm() as db, db.begin():
        with pytest.raises(Exception, match="سند"):
            await approvals.set_eligibility(
                db,
                workspace_id=ws.id,
                actor_id="t",
                contact_id=s.contact,
                status="allowed",
                basis="",
            )
    assert await count(sm, OutboundCommand) == 0


async def test_concurrent_approvals_create_one_command(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    """T10: اعتمادان متزامنان من المالك والمراجع → أمر إرسال واحد ومهمة واحدة."""
    s = await seed_draft(sm, ws)
    results = await asyncio.gather(
        approve(sm, ws, s.draft), approve(sm, ws, s.draft, reviewer=True)
    )
    assert sorted(created for _, created in results) == [False, True]
    assert await count(sm, OutboundCommand) == 1
    assert await count(sm, Approval, Approval.decision == "approve") == 1
    assert await count(sm, Job, Job.kind == "send_outbound") == 1


async def test_edit_after_approval_invalidates_it(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    """T11: تعديل النص بعد الاعتماد يبطل الاعتماد ويلغي الأمر؛ لا يُرسل الإصدار القديم ولا الجديد."""
    s = await seed_draft(sm, ws)
    await approve(sm, ws, s.draft)
    async with sm() as db, db.begin():
        d = await approvals.edit_draft(
            db,
            workspace_id=ws.id,
            actor_id=f"user:{ws.owner.auth_user_id}",
            draft_id=s.draft,
            revision=1,
            subject="موضوع معدل",
            body="نص معدل.\nلإيقاف التواصل ردوا بكلمة إيقاف.",
        )
        assert d.revision == 2 and d.status == "pending_review"
    result = await run_send(sm, deps(sm))
    assert result is not None and result["status"] == "canceled"
    assert FakeMailbox.outbox == []
    async with sm() as db:
        approval = (await db.execute(select(Approval))).scalar_one()
    assert approval.status == "superseded"
    # قرار على إصدار قديم يُرفض
    async with sm() as db, db.begin():
        d = await db.get(Draft, s.draft)
        assert d is not None
        with pytest.raises(Conflict):
            await approvals.decide(
                db,
                make_settings(),
                workspace_id=ws.id,
                actor_id=f"user:{ws.owner.auth_user_id}",
                auth_user_id=ws.owner.auth_user_id,
                draft_id=s.draft,
                revision=1,
                content_hash=d.content_hash,
                decision="approve",
            )


async def test_approved_email_is_sent_once_and_recorded(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    s = await seed_draft(sm, ws)
    await approve(sm, ws, s.draft)
    result = await run_send(sm, deps(sm))
    assert result is not None and result["status"] == "sent", result
    assert len(FakeMailbox.outbox) == 1
    sent = FakeMailbox.outbox[0]
    assert sent["To"] == s.value and "إيقاف" in sent.get_content()
    async with sm() as db:
        cmd = (await db.execute(select(OutboundCommand))).scalar_one()
        draft = await db.get(Draft, s.draft)
        opp = await db.get(Opportunity, s.opportunity)
        msg = (await db.execute(select(Message))).scalar_one()
        approval = (await db.execute(select(Approval))).scalar_one()
    assert cmd.status == "sent" and cmd.internet_message_id == str(sent["Message-ID"])
    assert draft is not None and draft.status == "sent"
    assert opp is not None and opp.status == "contacted" and opp.last_contacted_at is not None
    assert msg.direction == "outbound" and msg.internet_message_id == cmd.internet_message_id
    assert approval.status == "consumed"
    assert await run_send(sm, deps(sm)) is None  # لا مهمة ثانية


async def test_unknown_delivery_is_never_resent_blindly(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    """T13: انقطاع بعد DATA → unknown_delivery؛ إعادة المحاولة لا ترسل، والمصالحة بشرية."""
    s = await seed_draft(sm, ws)
    await approve(sm, ws, s.draft)
    FakeMailbox.fail_mode = "unknown_delivery"
    result = await run_send(sm, deps(sm))
    assert result is not None and result["status"] == "unknown_delivery"
    FakeMailbox.fail_mode = None
    async with sm() as db, db.begin():
        await queue.enqueue(
            db,
            kind="send_outbound",
            workspace_id=ws.id,
            payload={"draft_id": str(s.draft), "revision": 1},
            idempotency_key="retry-by-hand",
        )
    again = await run_send(sm, deps(sm))
    assert again is not None and again["status"] == "unknown_delivery"
    assert len(FakeMailbox.outbox) == 1  # الرسالة الأولى فقط
    async with sm() as db, db.begin():
        cmd = (await db.execute(select(OutboundCommand))).scalar_one()
        await approvals.resolve_unknown_delivery(
            db, workspace_id=ws.id, actor_id="user:x", command_id=cmd.id, outcome="sent"
        )
    async with sm() as db:
        cmd = (await db.execute(select(OutboundCommand))).scalar_one()
        draft = await db.get(Draft, s.draft)
    assert cmd.status == "sent" and draft is not None and draft.status == "sent"


async def test_worker_crash_mid_send_becomes_unknown(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    s = await seed_draft(sm, ws)
    await approve(sm, ws, s.draft)
    async with sm() as db, db.begin():
        cmd = (await db.execute(select(OutboundCommand))).scalar_one()
        cmd.status = "sending"  # عامل سابق مات بعد وضع «قيد الإرسال»
    result = await run_send(sm, deps(sm))
    assert result is not None and result["status"] == "unknown_delivery"
    assert FakeMailbox.outbox == []


async def test_paused_product_before_send_cancels(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    """T12: إيقاف المنتج بعد الاعتماد وقبل الإرسال يمنع الإرسال ويعيد المسودة للمراجعة."""
    s = await seed_draft(sm, ws)
    await approve(sm, ws, s.draft)
    async with sm() as db, db.begin():
        product = await db.get(Product, s.product)
        assert product is not None
        product.status = "paused"
    result = await run_send(sm, deps(sm))
    assert result is not None and result["status"] == "canceled"
    assert FakeMailbox.outbox == []
    async with sm() as db:
        draft = await db.get(Draft, s.draft)
    assert draft is not None and draft.status == "stale"


async def test_suppression_after_approval_blocks_send(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    s = await seed_draft(sm, ws)
    await approve(sm, ws, s.draft)
    async with sm() as db, db.begin():
        await suppress_contact(
            db,
            deps(sm),
            workspace_id=ws.id,
            email=s.value,
            company_id=s.company,
            reason="طلب إيقاف",
        )
    result = await run_send(sm, deps(sm))
    assert result is not None and result["status"] == "canceled"
    assert FakeMailbox.outbox == []


async def test_pause_outbound_holds_the_job(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    s = await seed_draft(sm, ws)
    async with sm() as db, db.begin():
        await integ.save_config(
            db,
            ws.id,
            "operations",
            integ.OperationsConfig(pause_outbound=True),
            version=0,
            actor_id="t",
        )
    await approve(sm, ws, s.draft)
    result = await run_send(sm, deps(sm))
    assert result is not None and result["reason"] == "paused"
    assert FakeMailbox.outbox == []
    async with sm() as db:
        job = (await db.execute(select(Job).where(Job.kind == "send_outbound"))).scalar_one()
        cmd = (await db.execute(select(OutboundCommand))).scalar_one()
    assert job.status in ("queued", "retry_scheduled") and cmd.status == "pending"


async def test_real_mailbox_needs_outbound_enabled(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    """صندوق حقيقي (SMTP) لا يرسل ما دام OUTBOUND_ENABLED=false."""
    from app.services.channels import TEST_HOOKS

    s = await seed_draft(sm, ws)
    async with sm() as db, db.begin():
        await integ.save_config(
            db,
            ws.id,
            "mail",
            integ.MailConfig(
                provider="smtp", smtp_username="sales@example.com", from_address="sales@example.com"
            ),
            version=0,
            actor_id="t",
        )
    await approve(sm, ws, s.draft)
    TEST_HOOKS["mailbox"] = FakeMailbox()
    try:
        result = await run_send(sm, deps(sm))
    finally:
        TEST_HOOKS["mailbox"] = None
    assert result is not None and result["reason"] == "outbound_disabled"
    assert FakeMailbox.outbox == []


async def test_whatsapp_is_manual_after_approval(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    s = await seed_draft(sm, ws, channel="whatsapp", value="+966500000001", allowed=False)
    async with sm() as db, db.begin():
        with pytest.raises(Conflict):
            await approvals.record_manual_contact(
                db, workspace_id=ws.id, actor_id="user:x", draft_id=s.draft, note=""
            )
    await approve(sm, ws, s.draft)
    assert await count(sm, OutboundCommand) == 0  # لا إرسال آلي عبر واتساب
    async with sm() as db, db.begin():
        await approvals.record_manual_contact(
            db,
            workspace_id=ws.id,
            actor_id=f"user:{ws.owner.auth_user_id}",
            draft_id=s.draft,
            note="أُرسلت",
        )
    async with sm() as db:
        draft = await db.get(Draft, s.draft)
        opp = await db.get(Opportunity, s.opportunity)
    assert draft is not None and draft.status == "sent"
    assert opp is not None and opp.status == "contacted"
    assert await count(sm, ManualContact) == 1
    assert await count(sm, Message, Message.channel == "whatsapp") == 1
