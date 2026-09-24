"""اكتشاف عبر خرائط Google: place ID فقط يُخزن، والاسم والأدلة من موقع المنشأة نفسها."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Company, CompanyIdentifier, Source
from app.db.models_ops import RunEvent, UsageLedger
from app.db.models_sales import Evidence, Opportunity
from app.services import integrations as integ
from app.services.channels import TEST_HOOKS
from app.services.secrets import set_secret
from app.workflows.common import Deps
from app.workflows.discovery import run_discovery
from tests.conftest import WorkspaceFixture, make_settings
from tests.integration.test_pipeline import fake_net, setup_catalog

PLACES = {
    "places": [
        {
            "id": "ChIJ-waha",
            "websiteUri": "https://clinic-waha.example/",
            "types": ["dentist"],
            "businessStatus": "OPERATIONAL",
        },
        {"id": "ChIJ-nosite", "types": ["dentist"], "businessStatus": "OPERATIONAL"},
        {
            "id": "ChIJ-closed",
            "websiteUri": "https://old.example/",
            "businessStatus": "CLOSED_PERMANENTLY",
        },
        {
            "id": "ChIJ-social",
            "websiteUri": "https://www.facebook.com/someclinic",
            "businessStatus": "OPERATIONAL",
        },
    ]
}


@pytest.fixture
def places_calls() -> Iterator[list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "mask": request.headers.get("X-Goog-FieldMask")})
        return httpx.Response(200, json=PLACES)

    TEST_HOOKS["places_transport"] = httpx.MockTransport(handler)
    yield calls
    TEST_HOOKS["places_transport"] = None


async def maps_source(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture, *, price: str | None = "0"
) -> None:
    await setup_catalog(sm, ws)
    async with sm() as db, db.begin():
        await db.execute(
            update(Source).values(kind="google_maps", connector_key="google_maps", name="خرائط")
        )
        await set_secret(db, make_settings(), ws.id, "google_maps_api_key", "maps-test-key", "t")
        await integ.save_config(
            db,
            ws.id,
            "search",
            integ.SearchConfig(
                maps_provider="google",
                maps_price_per_1k_requests=None if price is None else price,
            ),
            version=0,
            actor_id="t",
        )


async def test_maps_discovery_stores_place_id_and_reads_business_site(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture, places_calls: list[dict[str, Any]]
) -> None:
    net = fake_net()
    await maps_source(sm, ws)
    d = Deps(settings=make_settings(), sm=sm, resolver=net.resolve, transport=net.transport)
    result = await run_discovery(d, ws.id)
    assert result["status"] == "completed", result
    assert result["stats"]["created"] == 1 and result["stats"]["opportunities"] == 1
    assert places_calls and "displayName" not in (places_calls[0]["mask"] or "")
    async with sm() as db:
        company = (await db.execute(select(Company))).scalar_one()
        idents = {
            (i.kind, i.normalized_value)
            for i in (await db.execute(select(CompanyIdentifier))).scalars()
        }
        evidence = list((await db.execute(select(Evidence))).scalars())
        usage = list(
            (await db.execute(select(UsageLedger).where(UsageLedger.kind == "search"))).scalars()
        )
        events = [e.sanitized_summary for e in (await db.execute(select(RunEvent))).scalars()]
        opp = (await db.execute(select(Opportunity))).scalar_one()
    # الاسم من عنوان موقع المنشأة، لا من Google
    assert "الواحة" in company.display_name and company.domain == "clinic-waha.example"
    assert ("google_place", "ChIJ-waha") in idents
    assert not any(
        "ChIJ-nosite" in v or "ChIJ-closed" in v or "ChIJ-social" in v for _, v in idents
    )
    assert evidence and all(
        e.url and e.url.startswith("https://clinic-waha.example") for e in evidence
    )
    assert all("google" not in (e.url or "") for e in evidence)
    assert usage and usage[0].provider == "google_places" and usage[0].request_count == 1
    summary = next(e for e in events if e.startswith("خرائط Google"))
    assert "1 بموقع قُرئ" in summary and "2 بلا موقع خاص" in summary and "1 مغلق" in summary
    assert opp.preliminary_score > 0


async def test_known_place_is_not_refetched(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture, places_calls: list[dict[str, Any]]
) -> None:
    net = fake_net()
    await maps_source(sm, ws)
    async with sm() as db, db.begin():
        company = Company(workspace_id=ws.id, display_name="موجودة", normalized_name="موجوده")
        db.add(company)
        await db.flush()
        db.add(
            CompanyIdentifier(
                workspace_id=ws.id,
                company_id=company.id,
                kind="google_place",
                normalized_value="ChIJ-waha",
                strength="strong",
            )
        )
    d = Deps(settings=make_settings(), sm=sm, resolver=net.resolve, transport=net.transport)
    result = await run_discovery(d, ws.id)
    assert result["stats"]["created"] == 0
    assert not any(host == "clinic-waha.example" for _, host, _ in net.seen)  # لم يُقرأ الموقع ثانية


async def test_missing_price_blocks_paid_maps_before_any_call(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture, places_calls: list[dict[str, Any]]
) -> None:
    net = fake_net()
    await maps_source(sm, ws, price=None)
    d = Deps(settings=make_settings(), sm=sm, resolver=net.resolve, transport=net.transport)
    await run_discovery(d, ws.id)
    assert places_calls == []  # لا طلب بتكلفة غير معروفة
    async with sm() as db:
        events = [e.sanitized_summary for e in (await db.execute(select(RunEvent))).scalars()]
    assert any("سعر" in e for e in events)
