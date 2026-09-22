"""F-003: منتجات وفئات دون تعديل كود، تفعيل مشروط، تحقق تفاؤلي من الإصدار، وسجل تدقيق."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import AuditLog
from tests.conftest import ClientFactory, WorkspaceFixture


async def test_product_lifecycle_and_activation_rules(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    owner = await client_for(ws.owner)
    r = await owner.post("/api/products", json={"name": "منظم المواعيد", "priority": 4})
    assert r.status_code == 201
    product = r.json()
    assert product["status"] == "draft" and product["version"] == 1
    assert len(product["activation_problems"]) == 3

    r = await owner.post(
        f"/api/products/{product['id']}/status", json={"status": "active", "version": 1}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "activation_blocked"

    seg = (
        await owner.post("/api/segments", json={"name": "عيادات", "fit_rules": ["تعمل بالمواعيد"]})
    ).json()
    r = await owner.patch(
        f"/api/products/{product['id']}",
        json={
            "version": 1,
            "problem": "ضياع حجوزات بسبب الرد المتأخر",
            "capabilities": ["صفحة حجز", " صفحة حجز ", ""],
            "segments": [{"segment_id": seg["id"], "regions": ["الرياض"]}],
        },
    )
    assert r.status_code == 200
    product = r.json()
    assert product["version"] == 2 and product["capabilities"] == ["صفحة حجز"]
    assert product["activation_problems"] == []
    assert product["segments"][0]["regions"] == ["الرياض"]

    r = await owner.post(
        f"/api/products/{product['id']}/status", json={"status": "active", "version": 2}
    )
    assert r.status_code == 200 and r.json()["status"] == "active" and r.json()["version"] == 3

    # منتج نشط لا يُحفظ بوصف ناقص، ويبقى كما كان
    r = await owner.patch(f"/api/products/{product['id']}", json={"version": 3, "problem": ""})
    assert r.status_code == 422 and r.json()["error"]["code"] == "active_product_invalid"
    assert (await owner.get(f"/api/products/{product['id']}")).json()[
        "problem"
    ] == "ضياع حجوزات بسبب الرد المتأخر"

    async with sm() as db:
        actions = [
            a.action
            for a in (
                await db.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_id == product["id"])
                    .order_by(AuditLog.occurred_at)
                )
            ).scalars()
        ]
    assert actions == ["product.create", "product.update", "product.status.active"]


async def test_version_conflict_returns_409(
    client_for: ClientFactory, ws: WorkspaceFixture
) -> None:
    owner = await client_for(ws.owner)
    product = (await owner.post("/api/products", json={"name": "منتج"})).json()
    assert (
        await owner.patch(f"/api/products/{product['id']}", json={"version": 1, "summary": "أول"})
    ).status_code == 200
    r = await owner.patch(f"/api/products/{product['id']}", json={"version": 1, "summary": "متأخر"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_conflict"
    assert "حدّث الصفحة" in r.json()["error"]["message"]


async def test_invalid_input_returns_422_in_arabic(
    client_for: ClientFactory, ws: WorkspaceFixture
) -> None:
    owner = await client_for(ws.owner)
    r = await owner.post(
        "/api/products", json={"name": "", "priority": 9, "demo_url": "http://127.0.0.1/"}
    )
    body = r.json()["error"]
    assert r.status_code == 422 and body["code"] == "invalid_input"
    assert set(body["details"]["fields"]) >= {"name", "priority", "demo_url"}
    assert "داخلي" in body["details"]["fields"]["demo_url"]
    r = await owner.post("/api/products", json={"name": "x", "unknown": 1})
    assert r.status_code == 422


async def test_duplicate_names_conflict(client_for: ClientFactory, ws: WorkspaceFixture) -> None:
    owner = await client_for(ws.owner)
    assert (await owner.post("/api/segments", json={"name": "متاجر"})).status_code == 201
    r = await owner.post("/api/segments", json={"name": "متاجر"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_name"


async def test_new_product_and_segment_do_not_raise_daily_quota(
    client_for: ClientFactory, ws: WorkspaceFixture, settings: Settings
) -> None:
    """T01 (جزء M1): الحصة إعداد على مستوى workspace وليست لكل منتج؛ الإضافة لا تغيرها."""
    owner = await client_for(ws.owner)
    before = settings.max_qualified_per_day
    for i in range(3):
        seg = (await owner.post("/api/segments", json={"name": f"فئة {i}"})).json()
        await owner.post(
            "/api/products", json={"name": f"منتج {i}", "segments": [{"segment_id": seg["id"]}]}
        )
    assert settings.max_qualified_per_day == before == 3


async def test_archived_segment_does_not_count_for_activation(
    client_for: ClientFactory, ws: WorkspaceFixture
) -> None:
    owner = await client_for(ws.owner)
    seg = (await owner.post("/api/segments", json={"name": "مؤقتة"})).json()
    product = (
        await owner.post(
            "/api/products",
            json={
                "name": "منتج",
                "problem": "مشكلة",
                "capabilities": ["خاصية"],
                "segments": [{"segment_id": seg["id"]}],
            },
        )
    ).json()
    assert product["activation_problems"] == []
    await owner.patch(f"/api/segments/{seg['id']}", json={"version": 1, "status": "archived"})
    detail = (await owner.get(f"/api/products/{product['id']}")).json()
    assert any("فئة" in p for p in detail["activation_problems"])
