"""الدورة الكاملة عبر Hostinger Mail API: اعتماد ← إرسال HTTPS ← Message-ID محفوظ ← مزامنة الردود مجدولة."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Job
from app.db.models_sales import Draft, OutboundCommand
from app.jobs.scheduler import schedule_due
from app.services import integrations as integ
from app.services.channels import TEST_HOOKS, mail_context
from app.services.secrets import set_secret
from app.workflows.common import Deps
from tests.conftest import WorkspaceFixture, make_settings
from tests.sales_fixtures import approve, run_send, seed_draft


@pytest.fixture
def api() -> Iterator[list[dict[str, Any]]]:
    sends: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/me"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "orderResourceId": "OR1",
                        "mailboxes": [{"resourceId": "AC9", "address": "sales@acme.example"}],
                    }
                },
            )
        if path.endswith("/send"):
            sends.append(json.loads(req.content))
            return httpx.Response(204)
        if "/folders/" in path:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "uid": 5,
                            "subject": "تنظيم مواعيد العيادة",
                            "to": [{"name": "", "address": "buyer@clinic-waha.example"}],
                            "messageId": "<api-sent-1@hostinger>",
                        }
                    ],
                    "pagination": {},
                },
            )
        return httpx.Response(404)

    TEST_HOOKS["hostinger_transport"] = httpx.MockTransport(handler)
    yield sends
    TEST_HOOKS["hostinger_transport"] = None


async def configure(sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture) -> None:
    async with sm() as db, db.begin():
        await integ.save_config(
            db,
            ws.id,
            "mail",
            integ.MailConfig(
                provider="hostinger_api", smtp_username="sales@acme.example", from_name="أكمي"
            ),
            version=0,
            actor_id="t",
        )
        await set_secret(db, make_settings(), ws.id, "hostinger_mail_api_key", "tok-live", "t")


async def test_approved_email_goes_out_via_api_and_keeps_threading_id(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture, api: list[dict[str, Any]]
) -> None:
    s = await seed_draft(sm, ws)
    await configure(sm, ws)
    await approve(sm, ws, s.draft)
    result = await run_send(sm, Deps(settings=make_settings(outbound_enabled=True), sm=sm))
    assert result is not None and result["status"] == "sent", result
    assert api == [
        {
            "to": ["buyer@clinic-waha.example"],
            "subject": "تنظيم مواعيد العيادة",
            "text": api[0]["text"],
            "displayName": "أكمي",
        }
    ]
    async with sm() as db:
        cmd = (await db.execute(select(OutboundCommand))).scalar_one()
        draft = await db.get(Draft, s.draft)
    assert cmd.provider == "hostinger_api" and cmd.internet_message_id == "<api-sent-1@hostinger>"
    assert draft is not None and draft.status == "sent"


async def test_real_provider_still_needs_outbound_enabled(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture, api: list[dict[str, Any]]
) -> None:
    s = await seed_draft(sm, ws)
    await configure(sm, ws)
    await approve(sm, ws, s.draft)
    result = await run_send(sm, Deps(settings=make_settings(), sm=sm))
    assert result is not None and result["reason"] == "outbound_disabled"
    assert api == []  # لا طلب إرسال


async def test_missing_token_blocks_and_reply_sync_is_scheduled(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    async with sm() as db, db.begin():
        await integ.save_config(
            db,
            ws.id,
            "mail",
            integ.MailConfig(provider="hostinger_api", smtp_username="sales@acme.example"),
            version=0,
            actor_id="t",
        )
    async with sm() as db:
        from app.services.channels import ChannelNotReady

        with pytest.raises(ChannelNotReady, match="رمز Hostinger Mail API"):
            await mail_context(db, make_settings(), ws.id)
    await schedule_due(sm, make_settings())
    async with sm() as db:
        kinds = {j.kind for j in (await db.execute(select(Job))).scalars()}
    assert "mail_sync" in kinds  # الردود تُقرأ عبر IMAP لهذا المزود أيضًا
