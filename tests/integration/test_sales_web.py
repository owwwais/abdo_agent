"""صفحات المبيعات وواجهة JSON (SPEC §12): الموافقات والفرص والمحادثات والتشغيل والتحكم والعزل."""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Job
from app.db.models_sales import Draft, OutboundCommand
from app.services import integrations as integ
from tests.conftest import ClientFactory, WorkspaceFactory, WorkspaceFixture, make_settings
from tests.sales_fixtures import seed_draft


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "csrf token missing"
    return m.group(1)


async def test_pages_render_for_members(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture, client_for: ClientFactory
) -> None:
    s = await seed_draft(sm, ws, allowed=False)
    client = await client_for(ws.reviewer)
    for path in (
        "/",
        "/approvals",
        "/opportunities",
        f"/opportunities/{s.opportunity}",
        "/conversations",
        "/runs",
    ):
        r = await client.get(path)
        assert r.status_code == 200, (path, r.text[:300])
    page = (await client.get("/approvals")).text
    assert "مجمع عيادات الواحة" in page and "أهلية التواصل" in page
    assert re.search(r'value="approve">\s*<button class="btn" type="submit"\s+disabled', page)


async def test_form_flow_eligibility_then_approve(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture, client_for: ClientFactory
) -> None:
    s = await seed_draft(sm, ws, allowed=False)
    client = await client_for(ws.reviewer, csrf=False)
    token = csrf_of((await client.get("/approvals")).text)
    missing_basis = await client.post(
        f"/contacts/{s.contact}/eligibility",
        data={"csrf_token": token, "status": "allowed", "basis": ""},
    )
    assert missing_basis.status_code == 422 and "السند" in missing_basis.text
    ok = await client.post(
        f"/contacts/{s.contact}/eligibility",
        data={
            "csrf_token": token,
            "status": "allowed",
            "basis": "بريد عمل منشور",
            "back": "/approvals",
        },
    )
    assert ok.status_code == 303
    async with sm() as db:
        d = await db.get(Draft, s.draft)
    assert d is not None
    decided = await client.post(
        f"/approvals/{s.draft}/decision",
        data={
            "csrf_token": token,
            "decision": "approve",
            "revision": "1",
            "content_hash": d.content_hash,
        },
    )
    assert decided.status_code == 303 and "ok=approve" in decided.headers["location"]
    async with sm() as db:
        assert (
            await db.execute(select(func.count()).select_from(OutboundCommand))
        ).scalar_one() == 1
    no_csrf = await client.post(
        f"/approvals/{s.draft}/decision", data={"decision": "reject", "revision": "1"}
    )
    assert no_csrf.status_code == 403


async def test_api_decision_is_idempotent_and_edit_checks_revision(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture, client_for: ClientFactory
) -> None:
    s = await seed_draft(sm, ws)
    client = await client_for(ws.owner)
    listing = (await client.get("/api/opportunities")).json()
    assert listing["total"] == 1 and listing["items"][0]["company_name"] == "مجمع عيادات الواحة"
    detail = (await client.get(f"/api/opportunities/{s.opportunity}")).json()
    draft = detail["drafts"][0]
    assert detail["contacts"][0]["value_masked"] != s.value  # القيمة مقنّعة في الواجهة البرمجية
    body = {"decision": "approve", "revision": 1, "content_hash": draft["content_hash"]}
    first = await client.post(f"/api/approvals/{s.draft}/decision", json=body)
    second = await client.post(f"/api/approvals/{s.draft}/decision", json=body)
    assert first.json()["created"] is True and second.json()["created"] is False
    stale = await client.patch(
        f"/api/drafts/{s.draft}", json={"revision": 5, "subject": "x", "body": "y"}
    )
    assert stale.status_code == 409
    edited = await client.patch(
        f"/api/drafts/{s.draft}", json={"revision": 1, "subject": "جديد", "body": "نص جديد"}
    )
    assert edited.status_code == 200 and edited.json()["revision"] == 2
    async with sm() as db:
        cmd = (await db.execute(select(OutboundCommand))).scalar_one()
    assert cmd.status == "canceled"


async def test_control_and_manual_runs_are_owner_only(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture, client_for: ClientFactory
) -> None:
    reviewer = await client_for(ws.reviewer)
    assert (await reviewer.post("/api/control", json={"pause_outbound": True})).status_code == 403
    owner = await client_for(ws.owner)
    r = await owner.post("/api/control", json={"pause_outbound": True})
    assert r.status_code == 200 and r.json()["pause_outbound"] is True
    async with sm() as db:
        ops = await integ.get_config(
            db, make_settings(), ws.id, "operations", integ.OperationsConfig
        )
    assert ops.pause_outbound and not ops.pause_discovery
    html_owner = await client_for(ws.owner, csrf=False)
    token = csrf_of((await html_owner.get("/runs")).text)
    for _ in range(2):
        started = await html_owner.post(
            "/runs/start", data={"csrf_token": token, "kind": "discover"}
        )
        assert started.status_code == 303
    async with sm() as db:
        assert (
            await db.execute(
                select(func.count()).select_from(Job).where(Job.kind == "discover_daily")
            )
        ).scalar_one() == 1
    html_reviewer = await client_for(ws.reviewer, csrf=False)
    rtoken = csrf_of((await html_reviewer.get("/runs")).text)
    denied = await html_reviewer.post(
        "/runs/start", data={"csrf_token": rtoken, "kind": "discover"}
    )
    assert denied.status_code == 403


async def test_other_workspace_cannot_see_sales_data(
    sm: async_sessionmaker[AsyncSession],
    ws: WorkspaceFixture,
    make_workspace: WorkspaceFactory,
    client_for: ClientFactory,
) -> None:
    s = await seed_draft(sm, ws)
    other = await make_workspace(name="جهة أخرى")
    client = await client_for(other.owner)
    assert (await client.get(f"/api/opportunities/{s.opportunity}")).status_code == 404
    assert (await client.get(f"/opportunities/{s.opportunity}")).status_code == 404
    assert (await client.get("/api/opportunities")).json()["total"] == 0
    assert "مجمع عيادات الواحة" not in (await client.get("/approvals")).text
    async with sm() as db:
        d = await db.get(Draft, s.draft)
    assert d is not None
    r = await client.post(
        f"/api/approvals/{s.draft}/decision",
        json={"decision": "approve", "revision": 1, "content_hash": d.content_hash},
    )
    assert r.status_code == 404


async def test_manual_contact_then_understanding_test(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture, client_for: ClientFactory
) -> None:
    s = await seed_draft(sm, ws)
    owner = await client_for(ws.owner, csrf=False)
    token = csrf_of((await owner.get(f"/companies/{s.company}")).text)
    bad = await owner.post(
        f"/companies/{s.company}/contacts",
        data={"csrf_token": token, "channel": "email", "value": "not-an-email"},
    )
    assert bad.status_code == 303 and "contact_error" in bad.headers["location"]
    ok = await owner.post(
        f"/companies/{s.company}/contacts",
        data={"csrf_token": token, "channel": "email", "value": "Sales@Clinic-Waha.example"},
    )
    assert ok.status_code == 303 and "ok=contact_added" in ok.headers["location"]
    page = (await owner.get(f"/companies/{s.company}")).text
    assert "غير معروفة" in page and "مسموح (موثق)" in page  # الجديدة غير موثقة، والأولى موثقة
    tested = await owner.post(f"/products/{s.product}/understanding", data={"csrf_token": token})
    assert tested.status_code == 303 and "ok=understanding" in tested.headers["location"]
    product_page = (await owner.get(f"/products/{s.product}")).text
    assert "المشكلة كما فهمها" in product_page and "ضياع الحجوزات" in product_page
    api = await client_for(ws.reviewer)
    assert (await api.post(f"/api/products/{s.product}/test")).status_code == 403
