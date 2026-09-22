"""F-004: المصدر يبدأ غير نشط، عينة توضح المفقود، رابط داخلي ممنوع قبل الاتصال، تفعيل بتأكيد المالك فقط.
يغطي T02 وT03 وT04 وT05 (محتوى غير موثوق) وT12 (جزئيًا: تغيير الإعداد يلغي التفعيل)."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import Job, SourceCheck
from app.jobs.handlers import HandlerContext
from app.jobs.worker import Worker
from tests.conftest import ClientFactory, WorkspaceFixture, make_settings
from tests.fakes import CLINIC_HTML, CONTACT_HTML, LOGIN_HTML, FakeNet

CONFIRM = {"policy_version": 1, "confirmations": [True, True, True]}


def clinic_net() -> FakeNet:
    net = FakeNet()
    net.add_site("clinic-test.example")
    net.add_site("www.clinic-test.example")
    net.route(
        "clinic-test.example",
        "/robots.txt",
        httpx.Response(
            200, headers={"content-type": "text/plain"}, text="User-agent: *\nDisallow: /private/\n"
        ),
    )
    net.html("clinic-test.example", "/", CLINIC_HTML)
    net.html("clinic-test.example", "/contact", CONTACT_HTML)
    return net


async def run_worker(
    sm: async_sessionmaker[AsyncSession],
    net: FakeNet | None = None,
    settings: Settings | None = None,
) -> int:
    ctx = HandlerContext(
        settings=settings or make_settings(),
        sessionmaker=sm,
        resolver=net.resolve if net else None,
        transport=net.transport if net else None,
    )
    return await Worker(ctx, worker_id="test-worker").drain()


async def create(client: httpx.AsyncClient, **body: Any) -> dict[str, Any]:
    r = await client.post("/api/sources", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def test_html_source_sample_then_owner_activation(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    owner = await client_for(ws.owner)
    src = await create(owner, name="موقع عيادة", kind="html", url="https://clinic-test.example/")
    assert src["status"] == "new"
    assert src["allowed_hosts"] == ["clinic-test.example", "www.clinic-test.example"]

    # لا تفعيل دون عينة (لا تفعيل صامت)
    r = await owner.post(f"/api/sources/{src['id']}/activate", json={"version": 1, **CONFIRM})
    assert r.status_code == 409 and r.json()["error"]["code"] == "sample_required"

    r = await owner.post(f"/api/sources/{src['id']}/test")
    assert r.status_code == 202
    again = await owner.post(f"/api/sources/{src['id']}/test")
    assert again.json()["job"]["id"] == r.json()["job"]["id"]  # لا تكرار لمهمة الفحص
    assert (await owner.get(f"/api/sources/{src['id']}")).json()["status"] == "validating"

    net = clinic_net()
    assert await run_worker(sm, net) == 1
    detail = (await owner.get(f"/api/sources/{src['id']}")).json()
    assert detail["status"] == "sample_ready"
    check = detail["latest_check"]
    assert check["status"] == "succeeded"
    assert {"name", "description", "phone", "email", "category", "website"} <= set(
        check["fields_found"]
    )
    record = check["summary"]["records"][0]
    assert record["fields"]["email"] == "info@clinic-test.example"
    assert check["request_count"] == 3  # robots + الصفحة + صفحة التواصل
    assert check["cost"] == {"amount": "0.000000", "currency": "USD"}
    assert "<" not in str(check["summary"])  # لا HTML خام في الملخص المحفوظ

    # T05: نص «تجاهل التعليمات» محتوى غير موثوق؛ لم يغير أي صلاحية ولم ينشئ مهام
    assert detail["status"] == "sample_ready" and detail["policy_confirmed_at"] is None
    async with sm() as db:
        jobs = (await db.execute(select(func.count()).select_from(Job))).scalar_one()
    assert jobs == 1

    r = await owner.post(
        f"/api/sources/{src['id']}/activate",
        json={"version": 1, "policy_version": 1, "confirmations": [True, False, True]},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "policy_not_confirmed"
    r = await owner.post(
        f"/api/sources/{src['id']}/activate",
        json={"version": 1, "policy_version": 99, "confirmations": [True, True, True]},
    )
    assert r.status_code == 409
    r = await owner.post(f"/api/sources/{src['id']}/activate", json={"version": 1, **CONFIRM})
    assert (
        r.status_code == 200 and r.json()["status"] == "active" and r.json()["policy_version"] == 1
    )

    # تغيير النطاقات يعيد المصدر إلى new ويلغي التأكيد
    r = await owner.patch(f"/api/sources/{src['id']}", json={"version": 1, "max_pages": 2})
    assert r.status_code == 200
    changed = r.json()
    assert (
        changed["status"] == "new"
        and changed["policy_confirmed_at"] is None
        and changed["version"] == 2
    )
    r = await owner.post(f"/api/sources/{src['id']}/activate", json={"version": 2, **CONFIRM})
    assert r.status_code == 409


async def test_internal_url_rejected_before_any_connection(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    owner = await client_for(ws.owner)
    for url in (
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest",
        "http://localhost:8000/",
        "http://[::1]/",
    ):
        r = await owner.post("/api/sources", json={"name": f"x {url}", "kind": "html", "url": url})
        assert r.status_code == 422, url
    # نطاق عام يُحل إلى عنوان خاص: يُرفض عند الفحص دون أي طلب HTTP
    src = await create(owner, name="rebind", kind="html", url="https://rebind.example/")
    await owner.post(f"/api/sources/{src['id']}/test")
    net = FakeNet(dns={"rebind.example": ["10.1.2.3"]})
    await run_worker(sm, net)
    detail = (await owner.get(f"/api/sources/{src['id']}")).json()
    assert detail["status"] == "restricted"
    assert detail["latest_check"]["request_count"] == 0
    assert net.seen == []


async def test_access_denied_and_login_wall_are_restricted(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    owner = await client_for(ws.owner)
    net = FakeNet()
    for host in ("denied.example", "portal.example"):
        net.add_site(host)
        net.route(host, "/robots.txt", httpx.Response(404))
    net.html("denied.example", "/", "forbidden", status=403)
    net.html("portal.example", "/", LOGIN_HTML)
    denied = await create(owner, name="denied", kind="html", url="https://denied.example/")
    portal = await create(owner, name="بوابة فرص", kind="portal", url="https://portal.example/")
    await owner.post(f"/api/sources/{denied['id']}/test")
    await owner.post(f"/api/sources/{portal['id']}/test")
    await run_worker(sm, net)
    d = (await owner.get(f"/api/sources/{denied['id']}")).json()
    p = (await owner.get(f"/api/sources/{portal['id']}")).json()
    assert d["status"] == "restricted" and "403" in d["status_reason"]
    assert p["status"] == "restricted" and p["latest_check"]["summary"]["code"] == "login_required"
    assert p["latest_check"]["summary"]["records"] == []


async def test_robots_disallow_restricts(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    owner = await client_for(ws.owner)
    net = clinic_net()
    src = await create(
        owner, name="private", kind="html", url="https://clinic-test.example/private/list"
    )
    await owner.post(f"/api/sources/{src['id']}/test")
    await run_worker(sm, net)
    detail = (await owner.get(f"/api/sources/{src['id']}")).json()
    assert (
        detail["status"] == "restricted"
        and detail["latest_check"]["summary"]["code"] == "robots_disallow"
    )
    assert ("clinic-test.example", "/private/list") not in [(s[1], s[2]) for s in net.seen]


async def test_unconfigured_connectors_need_setup_without_fake_results(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    """T03: مصدر يحتاج وصولًا غير متاح → needs_setup مع سبب وخطوة، دون بيانات مختلقة."""
    owner = await client_for(ws.owner)
    maps = await create(owner, name="خرائط", kind="google_maps")
    await owner.post(f"/api/sources/{maps['id']}/test")
    await run_worker(sm)
    detail = (await owner.get(f"/api/sources/{maps['id']}")).json()
    assert detail["status"] == "needs_setup"
    assert detail["latest_check"]["summary"]["records"] == []
    assert detail["latest_check"]["summary"]["next_step"]


async def test_fetch_disabled_env_reports_needs_setup(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    owner = await client_for(ws.owner)
    src = await create(owner, name="موقع", kind="html", url="https://clinic-test.example/")
    await owner.post(f"/api/sources/{src['id']}/test")
    await run_worker(sm, clinic_net(), settings=make_settings(source_fetch_enabled=False))
    detail = (await owner.get(f"/api/sources/{src['id']}")).json()
    assert detail["status"] == "needs_setup" and detail["latest_check"]["request_count"] == 0


async def test_fake_search_sample_is_labeled_synthetic(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    owner = await client_for(ws.owner)
    src = await create(owner, name="بحث", kind="web_search")
    assert src["connector_key"] == "web_search"
    await owner.post(f"/api/sources/{src['id']}/test")
    await run_worker(sm)
    check = (await owner.get(f"/api/sources/{src['id']}")).json()["latest_check"]
    assert check["summary"]["synthetic"] is True
    assert all(r["fields"]["name"].startswith("[اصطناعي]") for r in check["summary"]["records"])
    assert all(".example" in r["url"] for r in check["summary"]["records"])


async def test_manual_source_and_linkedin_never_fetch(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    owner = await client_for(ws.owner)
    r = await owner.post(
        "/api/sources",
        json={"name": "LinkedIn", "kind": "linkedin", "url": "https://evil.example/"},
    )
    assert r.status_code == 422
    li = await create(
        owner, name="LinkedIn", kind="linkedin", url="https://www.linkedin.com/company/x"
    )
    assert li["connector_key"] == "manual" and li["allowed_hosts"] == []
    await owner.post(f"/api/sources/{li['id']}/test")
    net = FakeNet()
    await run_worker(sm, net)
    assert net.seen == []
    assert (await owner.get(f"/api/sources/{li['id']}")).json()["status"] == "sample_ready"


async def test_stale_check_does_not_change_status(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    owner = await client_for(ws.owner)
    src = await create(owner, name="موقع", kind="html", url="https://clinic-test.example/")
    await owner.post(f"/api/sources/{src['id']}/test")
    # يتغير الإعداد قبل تشغيل العامل؛ العامل يفحص الإعداد الحالي ويربط النتيجة بإصداره
    await owner.patch(f"/api/sources/{src['id']}", json={"version": 1, "retention_days": 30})
    await run_worker(sm, clinic_net())
    detail = (await owner.get(f"/api/sources/{src['id']}")).json()
    assert detail["latest_check"]["config_version"] == detail["version"] == 2
    assert detail["status"] == "sample_ready"
    async with sm() as db:
        assert (await db.execute(select(func.count()).select_from(SourceCheck))).scalar_one() == 1


async def test_pause_and_resume_requires_reconfirmation(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    owner = await client_for(ws.owner)
    src = await create(owner, name="إحالات", kind="manual")
    await owner.post(f"/api/sources/{src['id']}/test")
    await run_worker(sm)
    assert (
        await owner.post(f"/api/sources/{src['id']}/activate", json={"version": 1, **CONFIRM})
    ).status_code == 200
    r = await owner.post(f"/api/sources/{src['id']}/pause", json={"version": 1})
    assert r.json()["status"] == "paused"
    r = await owner.post(f"/api/sources/{src['id']}/activate", json={"version": 1, **CONFIRM})
    assert r.json()["status"] == "active"


async def test_secret_values_never_accepted_as_credential_ref(
    client_for: ClientFactory, ws: WorkspaceFixture
) -> None:
    owner = await client_for(ws.owner)
    r = await owner.post(
        "/api/sources",
        json={"name": "api", "kind": "web_search", "credential_ref": "sk-live-abc123"},
    )
    assert r.status_code == 422 and "credential_ref" in r.json()["error"]["details"]["fields"]
    r = await owner.post(
        "/api/sources",
        json={"name": "api", "kind": "web_search", "credential_ref": "SEARCH_API_KEY"},
    )
    assert r.status_code == 201 and r.json()["credential_ref"] == "SEARCH_API_KEY"
