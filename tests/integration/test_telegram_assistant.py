"""مساعد تيليجرام: أوامر بلا نموذج، أسئلة حرة للأعضاء المربوطين فقط، قراءة فقط وبلا جهات اتصال."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Job, UserProfile
from app.db.models_ops import UsageLedger
from app.jobs import queue
from app.services import integrations as integ
from app.services.assistant import build_context
from app.services.channels import TEST_HOOKS, telegram_context
from app.services.telegram_bot import answer_job, handle_update
from app.workflows.common import Deps
from tests.conftest import WorkspaceFixture, make_settings
from tests.integration.test_inbound_channels import CHAT, FakeTelegram, enable_telegram
from tests.sales_fixtures import seed_draft

MEMBER = 555
STRANGER = 777


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    TEST_HOOKS["telegram_transport"] = None


def deps(sm: async_sessionmaker[AsyncSession]) -> Deps:
    return Deps(settings=make_settings(), sm=sm)


async def setup(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> tuple[FakeTelegram, Any]:
    fake = await enable_telegram(sm, ws)
    await seed_draft(sm, ws)
    async with sm() as db, db.begin():
        profile = await db.get(UserProfile, ws.reviewer.auth_user_id)
        assert profile is not None
        profile.telegram_user_id = MEMBER
        tg = await telegram_context(db, make_settings(), ws.id)
    assert tg.client is not None
    return fake, tg.client


_uid = iter(range(1000, 100000))


def msg(
    text: str, *, sender: int = MEMBER, chat: int | str = MEMBER, reply_to_bot: bool = False
) -> dict[str, Any]:
    m: dict[str, Any] = {
        "message_id": next(_uid),
        "text": text,
        "from": {"id": sender},
        "chat": {"id": int(chat), "type": "private" if int(chat) == sender else "supergroup"},
    }
    if reply_to_bot:
        m["reply_to_message"] = {"from": {"id": 42, "is_bot": True}}
    return {"update_id": next(_uid), "message": m}


def sent(fake: FakeTelegram) -> list[dict[str, Any]]:
    return [p for m, p in fake.calls if m == "sendMessage"]


async def jobs(sm: async_sessionmaker[AsyncSession]) -> list[Job]:
    async with sm() as db:
        return list((await db.execute(select(Job).where(Job.kind == "telegram_answer"))).scalars())


async def test_commands_answer_without_model_for_linked_members_only(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    fake, client = await setup(sm, ws)
    d = deps(sm)
    stranger = await handle_update(
        d, ws.id, msg("/today", sender=STRANGER, chat=STRANGER), client, CHAT
    )
    assert stranger["status"] == "forbidden_user" and "غير مربوط" in sent(fake)[-1]["text"]

    assert (await handle_update(d, ws.id, msg("/today"), client, CHAT))["status"] == "today"
    assert "بانتظار اعتمادك: 1" in sent(fake)[-1]["text"]
    assert (await handle_update(d, ws.id, msg("/pending"), client, CHAT))["status"] == "pending"
    reply = sent(fake)[-1]
    assert "مجمع عيادات الواحة" in reply["text"] and "reply_parameters" in reply
    assert (await handle_update(d, ws.id, msg("/help"), client, CHAT))["status"] == "help"
    assert await jobs(sm) == []  # لا نموذج للأوامر


async def test_group_only_answers_ask_or_replies_in_allowed_chat(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    fake, client = await setup(sm, ws)
    d = deps(sm)
    before = len(sent(fake))
    chatter = await handle_update(d, ws.id, msg("صباح الخير يا شباب", chat=CHAT), client, CHAT)
    other = await handle_update(d, ws.id, msg("/ask كم فرصة؟", chat="-100999"), client, CHAT)
    assert chatter["status"] == "ignored" and other["status"] == "ignored"
    assert len(sent(fake)) == before  # صمت تام
    asked = await handle_update(d, ws.id, msg("/ask@SalesBot كم فرصة؟", chat=CHAT), client, CHAT)
    replied = await handle_update(
        d, ws.id, msg("وماذا عن الأمس؟", chat=CHAT, reply_to_bot=True), client, CHAT
    )
    assert asked["status"] == "queued" and replied["status"] == "queued"
    assert [j.payload["question"] for j in await jobs(sm)] == ["كم فرصة؟", "وماذا عن الأمس؟"]
    assert "sendChatAction" in fake.methods()


async def test_private_question_is_answered_from_readonly_snapshot(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    fake, client = await setup(sm, ws)
    d = deps(sm)
    update = msg("ما حالة مجمع عيادات الواحة؟")
    assert (await handle_update(d, ws.id, update, client, CHAT))["status"] == "queued"
    job = (await jobs(sm))[0]
    result = await answer_job(d, ws.id, job.payload)
    assert result["status"] == "answered"
    answer = sent(fake)[-1]
    assert "مجمع عيادات الواحة" in answer["text"]
    assert answer["reply_parameters"]["message_id"] == update["message"]["message_id"]
    async with sm() as db:
        ctx = await build_context(db, make_settings(), ws.id, "ما حالة مجمع عيادات الواحة؟")
        categories = set((await db.execute(select(UsageLedger.category))).scalars())
    dumped = json.dumps(ctx, ensure_ascii=False)
    assert (
        ctx["companies_mentioned"] and ctx["companies_mentioned"][0]["name"] == "مجمع عيادات الواحة"
    )
    assert "buyer@clinic-waha.example" not in dumped  # لا قيم جهات اتصال للنموذج
    assert categories <= {"assistant"}


async def test_daily_limit_and_disable_switch(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    fake, client = await setup(sm, ws)
    d = deps(sm)
    async with sm() as db, db.begin():
        await integ.save_config(
            db,
            ws.id,
            "telegram",
            integ.TelegramConfig(enabled=True, chat_id=CHAT, assistant_daily_limit=1),
            version=1,
            actor_id="t",
        )
    assert (await handle_update(d, ws.id, msg("سؤال أول"), client, CHAT))["status"] == "queued"
    assert (await handle_update(d, ws.id, msg("سؤال ثانٍ"), client, CHAT))["status"] == "limit"
    assert "حد الأسئلة" in sent(fake)[-1]["text"]
    async with sm() as db, db.begin():
        await integ.save_config(
            db,
            ws.id,
            "telegram",
            integ.TelegramConfig(enabled=True, chat_id=CHAT, assistant_enabled=False),
            version=2,
            actor_id="t",
        )
    assert (await handle_update(d, ws.id, msg("سؤال ثالث"), client, CHAT))[
        "status"
    ] == "assistant_disabled"
    assert (await handle_update(d, ws.id, msg("/today"), client, CHAT))["status"] == "today"


async def test_worker_runs_the_answer_job(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    from app.jobs.handlers import HandlerContext
    from app.jobs.worker import Worker

    fake, client = await setup(sm, ws)
    await handle_update(deps(sm), ws.id, msg("كم مسودة بانتظاري؟"), client, CHAT)
    await Worker(
        HandlerContext(settings=make_settings(), sessionmaker=sm), worker_id="w-tg"
    ).drain()
    async with sm() as db:
        status = (
            await db.execute(select(Job.status).where(Job.kind == "telegram_answer"))
        ).scalar_one()
        count = (
            await db.execute(select(func.count()).select_from(Job).where(Job.status == "failed"))
        ).scalar_one()
    assert status == "completed" and count == 0
    assert "بانتظار اعتمادك: 1" in sent(fake)[-1]["text"]
    _ = queue  # الطابور نفسه هو ما يستهلكه العامل
