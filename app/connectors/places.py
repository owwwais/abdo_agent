"""خرائط Google عبر Places API (New) — Text Search. مؤشر اكتشاف، لا مصدر بيانات مخزنة.

الطلب: POST https://places.googleapis.com/v1/places:searchText مع X-Goog-Api-Key وX-Goog-FieldMask
(إلزامي؛ لا حقول افتراضية)، والجسم textQuery وlanguageCode وregionCode وpageSize (1–20).

سياسة Google: لا يجوز تخزين محتوى Places (الاسم، العنوان، الهاتف، الموقع…) إلا معرف المكان place ID
فيُخزن بلا حد. لذلك:
- نطلب أقل الحقول: id وwebsiteUri (Enterprise SKU: 1000 طلب مجاني شهريًا) وtypes وbusinessStatus.
- نخزن place ID فقط (معرف قوي لمنع التكرار). رابط الموقع ونوع النشاط يُستعملان عابرًا لقراءة موقع
  المنشأة نفسها، ومنه نأخذ الاسم والوصف والأدلة. المنشأة بلا موقع تُعد ولا تُخزن.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

import httpx

from app.connectors.base import SampleRecord, SampleResult, SourceConfig, ValidationReport
from app.connectors.search import SearchError
from app.connectors.simple import _FAKE_RESULTS

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = "places.id,places.websiteUri,places.types,places.businessStatus"
CLOSED = ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY")


@dataclass
class PlaceHit:
    place_id: str
    website: str | None  # عابر: لا يُخزن
    types: list[str] = field(default_factory=list)  # عابر: لا يُخزن
    business_status: str = ""
    synthetic: bool = False
    # للاصطناعي فقط: بيانات من عندنا، لا من Google.
    name: str = ""
    description: str = ""

    @property
    def open(self) -> bool:
        return self.business_status not in CLOSED


def maps_link(place_id: str) -> str:
    """رابط Google Maps يُبنى من place ID المخزن عند العرض (لا نخزن googleMapsUri)."""
    return f"https://www.google.com/maps/place/?q=place_id:{place_id}"


class PlacesClient(Protocol):
    name: str
    paid: bool

    async def text_search(self, query: str, *, count: int) -> list[PlaceHit]: ...


class GooglePlacesClient:
    name = "google_places"
    paid = True

    def __init__(
        self,
        api_key: str,
        *,
        language: str = "ar",
        region: str = "SA",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self._language = language
        self._region = region
        self._transport = transport

    async def text_search(self, query: str, *, count: int) -> list[PlaceHit]:
        body = {
            "textQuery": query[:400],
            "languageCode": self._language,
            "regionCode": self._region,
            "pageSize": max(1, min(count, 20)),
        }
        headers = {
            "X-Goog-Api-Key": self._key,
            "X-Goog-FieldMask": FIELD_MASK,
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=20.0) as client:
                resp = await client.post(PLACES_URL, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise SearchError(
                "تعذر الاتصال بـGoogle Places", retryable=True, code="network"
            ) from exc
        if resp.status_code in (401, 403):
            raise SearchError(
                "مفتاح Google Maps غير صالح، أو Places API (New) غير مفعلة، أو الفوترة غير مربوطة",
                code="auth",
            )
        if resp.status_code == 429:
            raise SearchError("تجاوز حد طلبات Google Places", retryable=True, code="rate_limit")
        if resp.status_code >= 400:
            raise SearchError(
                f"Google Places أعاد خطأ ({resp.status_code})", retryable=resp.status_code >= 500
            )
        try:
            places = resp.json().get("places", [])
        except ValueError as exc:
            raise SearchError("رد غير صالح من Google Places") from exc
        hits = []
        for p in places[:count]:
            pid = str(p.get("id", ""))[:300]
            if not pid:
                continue
            website = p.get("websiteUri")
            hits.append(
                PlaceHit(
                    place_id=pid,
                    website=str(website)[:2000]
                    if isinstance(website, str) and website.startswith(("http://", "https://"))
                    else None,
                    types=[str(t) for t in p.get("types", [])][:10],
                    business_status=str(p.get("businessStatus", "")),
                )
            )
        return hits


class FakePlacesClient:
    """أماكن اصطناعية للتطوير وdemo: معرفات وهمية وبيانات من عندنا، لا اتصال بـGoogle."""

    name = "fake_places"
    paid = False

    async def text_search(self, query: str, *, count: int) -> list[PlaceHit]:
        return [
            PlaceHit(
                place_id=f"fake-place-{i}",
                website=r["website"],
                types=["establishment"],
                business_status="OPERATIONAL",
                synthetic=True,
                name=r["name"],
                description=r["description"],
            )
            for i, r in enumerate(_FAKE_RESULTS[:count], 1)
        ]


class GoogleMapsConnector:
    """مصدر «خرائط Google»: العينة استعلام واحد، وتعرض ما يسمح بتخزينه فقط."""

    key = "google_maps"

    def __init__(self, client: PlacesClient) -> None:
        self.client = client

    async def validate(self, config: SourceConfig) -> ValidationReport:
        return ValidationReport(
            status="succeeded",
            code="ok",
            message="أماكن اصطناعية" if not self.client.paid else "Google Places API (New)",
        )

    async def sample(self, config: SourceConfig) -> SampleResult:
        query = str(config.extra.get("sample_query") or "عيادة أسنان الرياض")
        try:
            hits = await self.client.text_search(query, count=min(config.max_records, 10))
        except SearchError as exc:
            status = "needs_setup" if exc.code == "auth" else "failed"
            return SampleResult(status=status, code=exc.code, message=str(exc), request_count=1)
        usable = [h for h in hits if h.open and h.website]
        records = [
            SampleRecord(
                source_record_id=h.place_id,
                url=maps_link(h.place_id),
                fields={"place_id": h.place_id, "website": "متوفر"},
            )
            for h in usable
        ]
        synthetic = bool(hits) and all(h.synthetic for h in hits)
        no_site = sum(1 for h in hits if h.open and not h.website)
        closed = sum(1 for h in hits if not h.open)
        return SampleResult(
            status="succeeded" if hits else "failed",
            code="synthetic" if synthetic else ("ok" if hits else "no_records"),
            message=(
                f"{len(hits)} مكانًا للاستعلام «{query}»: {len(usable)} بموقع إلكتروني"
                f"، {no_site} بلا موقع، {closed} مغلق. "
                "نخزن معرف المكان فقط؛ الاسم والتفاصيل تُقرأ من موقع المنشأة نفسها"
                + (" (بيانات اصطناعية)" if synthetic else "")
            ),
            records=records,
            fields_found=["place_id", "website"] if records else [],
            fields_missing=["name", "phone", "email"],
            request_count=0 if synthetic else 1,
            cost_amount=None if self.client.paid else Decimal("0"),
            synthetic=synthetic,
        )
