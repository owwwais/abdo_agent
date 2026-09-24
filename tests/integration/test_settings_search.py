"""تبويب البحث: Tavily وخرائط Google من الواجهة، والمفاتيح مشفرة ولا تُعرض."""

from __future__ import annotations

import re

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models_ops import IntegrationSecret
from app.services import integrations as integ
from app.services.channels import TEST_HOOKS, maps_context, search_context
from tests.conftest import ClientFactory, WorkspaceFixture, make_settings


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m
    return m.group(1)


async def test_save_tavily_and_maps_then_test_maps(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture, client_for: ClientFactory
) -> None:
    client = await client_for(ws.owner, csrf=False)
    page = (await client.get("/settings?tab=search")).text
    assert "Tavily" in page and "خرائط Google" in page and "معرف المكان فقط" in page
    token = csrf_of(page)
    saved = await client.post(
        "/settings/search",
        data={
            "csrf_token": token,
            "version": "0",
            "provider": "tavily",
            "secret_tavily_api_key": "tvly-SECRET-VALUE-123",
            "price_per_1k_requests": "0",
            "maps_provider": "google",
            "secret_google_maps_api_key": "AIza-SECRET-MAPS-456",
            "maps_price_per_1k_requests": "0",
            "maps_results_per_query": "10",
            "country": "SA",
            "search_lang": "ar",
        },
    )
    assert saved.status_code == 303, saved.text[:500]
    shown = (await client.get("/settings?tab=search")).text
    assert "tvly-SECRET" not in shown and "AIza-SECRET" not in shown
    async with sm() as db:
        blobs = [s.ciphertext for s in (await db.execute(select(IntegrationSecret))).scalars()]
        cfg = await integ.get_config(db, make_settings(), ws.id, "search", integ.SearchConfig)
        sctx = await search_context(db, make_settings(), ws.id)
        mctx = await maps_context(db, make_settings(), ws.id)
    assert len(blobs) == 2 and all(b"SECRET" not in b for b in blobs)
    assert cfg.provider == "tavily" and cfg.maps_results_per_query == 10
    assert sctx.client.name == "tavily" and mctx.client.name == "google_places"

    TEST_HOOKS["places_transport"] = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            json={"places": [{"id": "a", "websiteUri": "https://a.example"}, {"id": "b"}]},
        )
    )
    try:
        tested = await client.post("/settings/search/test-maps", data={"csrf_token": token})
    finally:
        TEST_HOOKS["places_transport"] = None
    assert tested.status_code == 303
    after = (await client.get("/settings?tab=search")).text
    assert "google_places: 2 أماكن، 1 منها بموقع إلكتروني" in after
