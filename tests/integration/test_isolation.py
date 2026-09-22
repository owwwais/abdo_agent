"""T19: بيانات workspace آخر ممنوعة قراءةً وتعديلًا واعتمادًا، وعلى مستوى القاعدة أيضًا."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ProductSegment
from tests.conftest import ClientFactory, WorkspaceFactory


async def test_other_workspace_cannot_read_or_modify(
    client_for: ClientFactory, make_workspace: WorkspaceFactory
) -> None:
    a = await make_workspace("أ")
    b = await make_workspace("ب")
    owner_a = await client_for(a.owner)
    owner_b = await client_for(b.owner)

    seg = (await owner_a.post("/api/segments", json={"name": "عيادات"})).json()
    product = (await owner_a.post("/api/products", json={"name": "منتج أ"})).json()
    source = (await owner_a.post("/api/sources", json={"name": "إحالات", "kind": "manual"})).json()

    for path in (
        f"/api/products/{product['id']}",
        f"/api/sources/{source['id']}",
        f"/products/{product['id']}",
        f"/sources/{source['id']}",
    ):
        r = await owner_b.get(path)
        assert r.status_code == 404, path

    assert (
        await owner_b.patch(f"/api/products/{product['id']}", json={"version": 1, "name": "اختراق"})
    ).status_code == 404
    assert (
        await owner_b.post(
            f"/api/products/{product['id']}/status", json={"status": "archived", "version": 1}
        )
    ).status_code == 404
    assert (
        await owner_b.patch(f"/api/segments/{seg['id']}", json={"version": 1, "name": "x"})
    ).status_code == 404
    assert (await owner_b.post(f"/api/sources/{source['id']}/test")).status_code == 404
    activate = {"version": 1, "policy_version": 1, "confirmations": [True, True, True]}
    assert (
        await owner_b.post(f"/api/sources/{source['id']}/activate", json=activate)
    ).status_code == 404

    # ربط منتج B بفئة من A مرفوض
    r = await owner_b.post(
        "/api/products", json={"name": "منتج ب", "segments": [{"segment_id": seg["id"]}]}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_segment"
    # الاستيراد إلى مصدر A من B مرفوض
    r = await owner_b.post("/api/imports/manual", json={"source_id": source["id"], "name": "جهة"})
    assert r.status_code == 404

    listed = (await owner_b.get("/api/products")).json()["items"]
    assert listed == []
    assert (await owner_a.get(f"/api/products/{product['id']}")).json()["name"] == "منتج أ"


async def test_database_rejects_cross_workspace_links(
    sm: async_sessionmaker[AsyncSession],
    client_for: ClientFactory,
    make_workspace: WorkspaceFactory,
) -> None:
    a = await make_workspace("أ")
    b = await make_workspace("ب")
    seg_a = (await (await client_for(a.owner)).post("/api/segments", json={"name": "فئة أ"})).json()
    prod_b = (
        await (await client_for(b.owner)).post("/api/products", json={"name": "منتج ب"})
    ).json()
    with pytest.raises(IntegrityError):
        async with sm() as db, db.begin():
            db.add(
                ProductSegment(workspace_id=b.id, product_id=prod_b["id"], segment_id=seg_a["id"])
            )
