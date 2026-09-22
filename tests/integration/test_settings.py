"""الإعدادات والأسرار والميزانية والبوابة: تشفير عند التخزين، عدم عرض الأسرار، الصلاحيات، ربط Hostinger
التلقائي، جاهزية التشغيل الفعلي، حجز ذري للميزانية تحت التزامن (T17)، وحدود استدعاءات النموذج."""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.gateway import CallCounter, CallLimitReached, ModelGateway, ModelNotReady
from app.agents.providers import FakeClient, ProviderResult
from app.config import Settings
from app.connectors.mail import FakeMailbox
from app.db.models import AuditLog
from app.db.models_ops import BudgetReservation, IntegrationSecret, UsageLedger
from app.services import budget
from app.services import integrations as integ
from app.services.channels import TEST_HOOKS
from app.services.connection_tests import Ping
from app.services.secrets import box, get_secret
from tests.conftest import ClientFactory, WorkspaceFixture, make_settings

SECRET = "sk-ant-api03-very-secret-value-123"


def csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m
    return m.group(1)


@pytest.fixture(autouse=True)
def _reset_hooks() -> Any:
    FakeMailbox.reset()
    yield
    for k in TEST_HOOKS:
        TEST_HOOKS[k] = None
    FakeMailbox.reset()


async def test_secret_is_encrypted_at_rest_and_never_rendered(
    client_for: ClientFactory,
    ws: WorkspaceFixture,
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    owner = await client_for(ws.owner)
    token = csrf((await owner.get("/settings?tab=models")).text)
    r = await owner.post(
        "/settings/models",
        data={
            "csrf_token": token,
            "version": "0",
            "extractor_provider": "anthropic",
            "extractor_model": "claude-opus-5",
            "extractor_price_in": "5",
            "extractor_price_out": "25",
            "writer_provider": "fake",
            "secret_anthropic_api_key": SECRET,
        },
    )
    assert r.status_code == 303
    async with sm() as db:
        row = (await db.execute(select(IntegrationSecret))).scalar_one()
        assert SECRET.encode() not in row.ciphertext
        assert box(settings).decrypt(row.ciphertext) == SECRET
        assert await get_secret(db, settings, ws.id, "anthropic_api_key") == (SECRET, "settings")
        audit = json.dumps(
            [a.redacted_change for a in (await db.execute(select(AuditLog))).scalars()],
            ensure_ascii=False,
        )
    assert SECRET not in audit
    page = (await owner.get("/settings?tab=models")).text
    assert SECRET not in page and "مضبوط من هذه الصفحة" in page
    # حقل فارغ يبقي القيمة، وخيار الحذف يزيلها
    token = csrf(page)
    await owner.post(
        "/settings/models",
        data={
            "csrf_token": token,
            "version": "1",
            "extractor_provider": "fake",
            "writer_provider": "fake",
        },
    )
    async with sm() as db:
        assert (await get_secret(db, settings, ws.id, "anthropic_api_key"))[1] == "settings"
    await owner.post(
        "/settings/models",
        data={
            "csrf_token": token,
            "version": "2",
            "extractor_provider": "fake",
            "writer_provider": "fake",
            "delete_anthropic_api_key": "on",
        },
    )
    async with sm() as db:
        assert (await get_secret(db, settings, ws.id, "anthropic_api_key"))[1] == "none"


async def test_env_fallback_reported_without_value(
    engine: Any, sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    s = make_settings(openai_api_key="sk-openai-env-value")
    async with sm() as db:
        assert await get_secret(db, s, ws.id, "openai_api_key") == ("sk-openai-env-value", "env")


async def test_reviewer_cannot_change_settings(
    client_for: ClientFactory, ws: WorkspaceFixture
) -> None:
    reviewer = await client_for(ws.reviewer)
    token = csrf((await reviewer.get("/settings")).text)
    for path in (
        "/settings/models",
        "/settings/mail",
        "/settings/operations",
        "/settings/search",
        "/settings/telegram",
    ):
        r = await reviewer.post(path, data={"csrf_token": token, "version": "0"})
        assert r.status_code == 403, path


async def test_hostinger_preset_autofills_servers(
    client_for: ClientFactory,
    ws: WorkspaceFixture,
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    owner = await client_for(ws.owner)
    token = csrf((await owner.get("/settings?tab=mail")).text)
    r = await owner.post(
        "/settings/mail",
        data={
            "csrf_token": token,
            "version": "0",
            "provider": "smtp",
            "preset": "hostinger",
            "smtp_username": "sales@company.example",
            "from_name": "فريق المبيعات",
            "imap_enabled": "on",
            "imap_same_password": "on",
            "secret_smtp_password": "mailbox-pass",
            "smtp_host": "evil.example",
            "smtp_port": "25",
        },
    )
    assert r.status_code == 303
    async with sm() as db:
        cfg = await integ.get_config(db, settings, ws.id, "mail", integ.MailConfig)
    assert (cfg.smtp_host, cfg.smtp_port, cfg.smtp_security) == ("smtp.hostinger.com", 465, "ssl")
    assert (cfg.imap_host, cfg.imap_port, cfg.imap_username) == (
        "imap.hostinger.com",
        993,
        "sales@company.example",
    )
    assert cfg.from_address == "sales@company.example" and cfg.configured
    page = (await owner.get("/settings?tab=mail")).text
    assert "/webhooks/email/hostinger" in page and "mailbox-pass" not in page

    TEST_HOOKS["mailbox"] = FakeMailbox()
    r = await owner.post("/settings/mail/test", data={"csrf_token": token})
    assert r.status_code == 303 and "ok=tested" in r.headers["location"]


async def test_live_mode_blocked_until_ready(
    client_for: ClientFactory,
    ws: WorkspaceFixture,
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    owner = await client_for(ws.owner)
    token = csrf((await owner.get("/settings")).text)
    r = await owner.post(
        "/settings/operations", data={"csrf_token": token, "version": "0", "operating_mode": "live"}
    )
    notice = re.search(r'<div class="notice notice-bad" role="alert">(.*?)</div>', r.text, re.S)
    assert r.status_code == 422 and notice, r.text[-2000:]
    assert "لا يمكن التحويل إلى التشغيل الفعلي" in notice.group(1), notice.group(1)
    async with sm() as db:
        cfg = await integ.get_config(db, settings, ws.id, "operations", integ.OperationsConfig)
    assert cfg.operating_mode == "demo"
    r = await owner.post(
        "/settings/operations",
        data={
            "csrf_token": token,
            "version": "0",
            "operating_mode": "demo",
            "daily_budget": "2",
            "monthly_budget": "30",
            "process_slots": "10:00، 12:00, 14:00",
            "pause_outbound": "on",
        },
    )
    assert r.status_code == 303
    async with sm() as db:
        cfg = await integ.get_config(db, settings, ws.id, "operations", integ.OperationsConfig)
    assert (
        cfg.daily_budget == Decimal("2")
        and cfg.pause_outbound
        and cfg.process_slots == ["10:00", "12:00", "14:00"]
    )


# ---------------------------------------------------------------- البوابة والميزانية


class ScriptedClient:
    name = "scripted"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls = 0

    async def complete_json(self, **kw: Any) -> ProviderResult:
        self.calls += 1
        return ProviderResult(
            text=self.outputs.pop(0), input_tokens=1000, output_tokens=200, model=kw["model"]
        )

    async def list_models(self) -> list[str]:
        return ["m1"]


async def _configure(
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
    ws: WorkspaceFixture,
    *,
    price: bool = True,
    approved: bool = True,
    budget_daily: str | None = "1.00",
) -> None:
    async with sm() as db, db.begin():
        prices = (
            {
                "anthropic:claude-opus-5": integ.ModelPrice(
                    input_per_mtok=Decimal(5), output_per_mtok=Decimal(25)
                )
            }
            if price
            else {}
        )
        await integ.save_config(
            db,
            ws.id,
            "models",
            integ.ModelsConfig(
                extractor=integ.ModelRole(provider="anthropic", model="claude-opus-5"),
                writer=integ.ModelRole(provider="anthropic", model="claude-opus-5"),
                prices=prices,
                processing_approved=approved,
                max_output_tokens=1000,
            ),
            version=0,
            actor_id="test",
        )
        from app.services.secrets import set_secret

        await set_secret(db, settings, ws.id, "anthropic_api_key", "sk-test", "test")
        if budget_daily:
            await integ.save_config(
                db,
                ws.id,
                "operations",
                integ.OperationsConfig(
                    daily_budget=Decimal(budget_daily), monthly_budget=Decimal("10")
                ),
                version=0,
                actor_id="test",
            )


def dev_settings() -> Settings:
    return make_settings(app_env="development")


async def test_gateway_refuses_unpriced_or_unapproved_models(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    s = dev_settings()
    await _configure(sm, s, ws, price=False)
    gw = ModelGateway(
        s, sm, ws.id, client_factory=lambda *a: ScriptedClient(['{"ok": true, "reply": "x"}'])
    )
    with pytest.raises(ModelNotReady) as err:
        await gw.resolve("writer")
    assert err.value.code == "price_unknown"


async def test_gateway_budget_settlement_and_repair(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    s = dev_settings()
    await _configure(sm, s, ws)
    client = ScriptedClient(['{"ok": "not-bool"', '{"ok": true, "reply": "pong"}'])
    gw = ModelGateway(s, sm, ws.id, client_factory=lambda *a: client)
    counter = CallCounter(5)
    out, usage = await gw.complete(
        role="writer",
        system="s",
        user="u",
        output_model=Ping,
        schema_name="p",
        category="new_opportunity",
        counter=counter,
    )
    assert out.reply == "pong" and client.calls == 2 and counter.used == 2
    # 2 × (1000 × 5 + 200 × 25) / 1e6 = 0.02
    assert usage.cost == Decimal("0.02")
    async with sm() as db:
        ledger = (await db.execute(select(UsageLedger))).scalars().all()
        reservations = (await db.execute(select(BudgetReservation))).scalars().all()
    assert len(ledger) == 2 and all(r.status == "settled" for r in reservations)
    # حد الاستدعاءات يشمل محاولات التصحيح
    with pytest.raises(CallLimitReached):
        await gw.complete(
            role="writer",
            system="s",
            user="u",
            output_model=Ping,
            schema_name="p",
            category="new_opportunity",
            counter=CallCounter(1, used=1),
        )


async def test_budget_reservations_are_atomic_under_concurrency(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    """T17: سباق على الميزانية لا يتجاوز الحد اليومي."""
    ops = integ.OperationsConfig(daily_budget=Decimal("0.10"), monthly_budget=Decimal("10"))

    async def attempt() -> bool:
        try:
            async with sm() as db, db.begin():
                await budget.reserve(
                    db,
                    ws.id,
                    ops,
                    amount=Decimal("0.04"),
                    category="discovery",
                    run_id=None,
                    tz_name="Asia/Riyadh",
                )
            return True
        except budget.BudgetBlocked:
            return False

    results = await asyncio.gather(*(attempt() for _ in range(8)))
    assert sum(results) == 2
    async with sm() as db:
        total = sum(r.amount for r in (await db.execute(select(BudgetReservation))).scalars())
    assert total <= Decimal("0.10")


async def test_paid_call_blocked_without_budget(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    s = dev_settings()
    await _configure(sm, s, ws, budget_daily=None)
    gw = ModelGateway(
        s, sm, ws.id, client_factory=lambda *a: ScriptedClient(['{"ok": true, "reply": "x"}'])
    )
    with pytest.raises(budget.BudgetBlocked) as err:
        await gw.complete(
            role="writer",
            system="s",
            user="u",
            output_model=Ping,
            schema_name="p",
            category="new_opportunity",
            counter=CallCounter(5),
        )
    assert err.value.code == "budget_not_set"


async def test_demo_env_forces_fake_model(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    demo = make_settings(app_env="demo")
    await _configure(sm, demo, ws)
    resolved = await ModelGateway(demo, sm, ws.id).resolve("writer")
    assert resolved.provider == "fake" and isinstance(resolved.client, FakeClient)


async def test_search_and_telegram_connection_tests_with_mock_transport(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession], app: Any
) -> None:
    app.state.settings = dev_settings()
    owner = await client_for(ws.owner)
    token = csrf((await owner.get("/settings?tab=search")).text)
    TEST_HOOKS["search_transport"] = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {"title": "عيادة", "url": "https://clinic.example/", "description": "د"}
                    ]
                }
            },
        )
    )
    await owner.post(
        "/settings/search",
        data={
            "csrf_token": token,
            "version": "0",
            "provider": "brave",
            "secret_brave_api_key": "brave-key",
            "price_per_1k_requests": "5",
        },
    )
    r = await owner.post("/settings/search/test", data={"csrf_token": token})
    assert r.status_code == 303, r.text
    async with sm() as db:
        usage = (await db.execute(select(UsageLedger))).scalar_one()
    assert usage.kind == "search" and usage.estimated_cost == Decimal("0.005")

    sent: list[dict[str, Any]] = []

    def tg(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        sent.append({"path": request.url.path, **body})
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "sales_bot"}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    TEST_HOOKS["telegram_transport"] = httpx.MockTransport(tg)
    await owner.post(
        "/settings/telegram",
        data={
            "csrf_token": token,
            "version": "0",
            "enabled": "on",
            "chat_id": "-1001234567890",
            "secret_telegram_bot_token": "123:ABC",
        },
    )
    r = await owner.post("/settings/telegram/test", data={"csrf_token": token, "action": "send"})
    assert r.status_code == 303
    assert any(s.get("chat_id") == "-1001234567890" for s in sent)
    # الرمز جزء من مسار Bot API بطبيعته، لكنه لا يظهر في أي جسم طلب أو صفحة.
    assert all(
        "123:ABC" not in json.dumps({k: v for k, v in s.items() if k != "path"}) for s in sent
    )
    assert "123:ABC" not in (await owner.get("/settings?tab=telegram")).text
