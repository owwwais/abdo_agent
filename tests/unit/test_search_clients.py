"""شكل طلبات Google Places (New) وTavily كما في الوثائق الرسمية، وسلوك الأخطاء، دون شبكة."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.connectors.base import SourceConfig
from app.connectors.places import (
    FIELD_MASK,
    PLACES_URL,
    FakePlacesClient,
    GoogleMapsConnector,
    GooglePlacesClient,
    maps_link,
)
from app.connectors.search import TAVILY_URL, SearchError, TavilySearchClient


def recorder(
    status: int, payload: dict[str, Any]
) -> tuple[list[httpx.Request], httpx.MockTransport]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload)

    return seen, httpx.MockTransport(handler)


async def test_places_request_uses_minimal_field_mask_and_parses() -> None:
    seen, transport = recorder(
        200,
        {
            "places": [
                {
                    "id": "ChIJ-open",
                    "websiteUri": "https://clinic.example/",
                    "types": ["dentist"],
                    "businessStatus": "OPERATIONAL",
                },
                {"id": "ChIJ-nosite", "businessStatus": "OPERATIONAL"},
                {
                    "id": "ChIJ-closed",
                    "websiteUri": "https://x.example",
                    "businessStatus": "CLOSED_PERMANENTLY",
                },
                {"websiteUri": "https://noid.example"},
            ]
        },
    )
    client = GooglePlacesClient("maps-key", language="ar", region="SA", transport=transport)
    hits = await client.text_search("عيادة أسنان الرياض", count=50)
    req = seen[0]
    assert req.method == "POST" and str(req.url) == PLACES_URL
    assert req.headers["X-Goog-Api-Key"] == "maps-key"
    # لا اسم ولا عنوان ولا هاتف: فقط ما نحتاجه عابرًا ومعرف المكان
    assert req.headers["X-Goog-FieldMask"] == FIELD_MASK
    assert "displayName" not in FIELD_MASK and "Phone" not in FIELD_MASK
    body = json.loads(req.content)
    assert body == {
        "textQuery": "عيادة أسنان الرياض",
        "languageCode": "ar",
        "regionCode": "SA",
        "pageSize": 20,
    }
    assert [h.place_id for h in hits] == ["ChIJ-open", "ChIJ-nosite", "ChIJ-closed"]
    assert hits[0].website == "https://clinic.example/" and hits[0].open
    assert hits[1].website is None
    assert not hits[2].open
    assert maps_link("ChIJ-open").endswith("place_id:ChIJ-open")


@pytest.mark.parametrize(
    ("status", "code"), [(403, "auth"), (429, "rate_limit"), (500, "search_error")]
)
async def test_places_errors_are_classified(status: int, code: str) -> None:
    _, transport = recorder(status, {"error": {"message": "x"}})
    with pytest.raises(SearchError) as exc:
        await GooglePlacesClient("k", transport=transport).text_search("q", count=5)
    assert exc.value.code == code


async def test_tavily_request_shape_and_quota_error() -> None:
    seen, transport = recorder(
        200,
        {
            "results": [
                {"title": "عيادة", "url": "https://c.example", "content": "حجز", "score": 0.9}
            ]
        },
    )
    hits = await TavilySearchClient("tvly-test", country="SA", transport=transport).search(
        "q", count=5
    )
    req = seen[0]
    assert req.method == "POST" and str(req.url) == TAVILY_URL
    assert req.headers["Authorization"] == "Bearer tvly-test"
    body = json.loads(req.content)
    assert body["search_depth"] == "basic" and body["max_results"] == 5
    assert body["topic"] == "general" and body["country"] == "saudi arabia"
    assert hits[0].url == "https://c.example" and hits[0].description == "حجز"
    _, over = recorder(432, {"detail": {"error": "plan limit"}})
    with pytest.raises(SearchError) as exc:
        await TavilySearchClient("k", transport=over).search("q", count=5)
    assert exc.value.code == "quota"


async def test_maps_sample_shows_only_storable_fields() -> None:
    import uuid

    config = SourceConfig(
        source_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        kind="google_maps",
        connector_key="google_maps",
        url=None,
        allowed_hosts=(),
        allowed_paths=(),
        max_pages=1,
        max_records=5,
        max_requests=1,
        timeout_seconds=10,
        credential_ref=None,
        store_raw=False,
        retention_days=90,
        config_version=1,
    )
    result = await GoogleMapsConnector(FakePlacesClient()).sample(config)
    assert result.status == "succeeded" and result.synthetic
    for rec in result.records:
        assert set(rec.fields) == {"place_id", "website"} and rec.fields["website"] == "متوفر"
    assert "معرف المكان فقط" in result.message
