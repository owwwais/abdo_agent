"""البحث على الويب: Brave وTavily (موصلان حقيقيان) وFakeSearch الاصطناعي.

Brave: GET https://api.search.brave.com/res/v1/web/search مع ترويسة X-Subscription-Token،
والمعاملات q وcount (≤20) وcountry وsearch_lang. النتائج في web.results[].title/url/description.

Tavily: POST https://api.tavily.com/search مع Authorization: Bearer، والجسم query وsearch_depth=basic
(رصيد واحد) وmax_results (≤20) وtopic=general وcountry (اسم الدولة بالإنجليزية). النتائج في
results[].title/url/content. الخطة المجانية 1000 رصيد شهريًا بلا بطاقة.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import httpx

from app.connectors.base import SampleRecord, SampleResult, SourceConfig, ValidationReport
from app.connectors.simple import _FAKE_RESULTS

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
TAVILY_URL = "https://api.tavily.com/search"
# Tavily يقبل اسم الدولة لا رمزها، ومع topic=general فقط.
_TAVILY_COUNTRIES = {
    "SA": "saudi arabia",
    "AE": "united arab emirates",
    "KW": "kuwait",
    "QA": "qatar",
    "BH": "bahrain",
    "OM": "oman",
    "EG": "egypt",
    "JO": "jordan",
}


class SearchError(Exception):
    def __init__(
        self, message: str, *, code: str = "search_error", retryable: bool = False
    ) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


@dataclass
class SearchHit:
    title: str
    url: str
    description: str
    synthetic: bool = False


class SearchClient(Protocol):
    name: str
    paid: bool

    async def search(self, query: str, *, count: int) -> list[SearchHit]: ...


class BraveSearchClient:
    name = "brave"
    paid = True

    def __init__(
        self,
        api_key: str,
        *,
        country: str = "SA",
        lang: str = "ar",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self._country = country
        self._lang = lang
        self._transport = transport

    async def search(self, query: str, *, count: int) -> list[SearchHit]:
        params = {
            "q": query[:400],
            "count": str(max(1, min(count, 20))),
            "country": self._country,
            "search_lang": self._lang,
            "safesearch": "moderate",
        }
        headers = {"X-Subscription-Token": self._key, "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=20.0) as client:
                resp = await client.get(BRAVE_URL, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise SearchError(
                "تعذر الاتصال بـBrave Search", retryable=True, code="network"
            ) from exc
        if resp.status_code in (401, 403):
            raise SearchError("مفتاح Brave Search غير صالح أو بلا صلاحية", code="auth")
        if resp.status_code == 429:
            raise SearchError("تجاوز حد طلبات Brave Search", retryable=True, code="rate_limit")
        if resp.status_code >= 400:
            raise SearchError(
                f"Brave Search أعاد خطأ ({resp.status_code})", retryable=resp.status_code >= 500
            )
        try:
            results = resp.json().get("web", {}).get("results", [])
        except ValueError as exc:
            raise SearchError("رد غير صالح من Brave Search") from exc
        hits = []
        for r in results[:count]:
            url = str(r.get("url", ""))
            if url.startswith(("http://", "https://")):
                hits.append(
                    SearchHit(
                        str(r.get("title", ""))[:300],
                        url[:2000],
                        str(r.get("description", ""))[:500],
                    )
                )
        return hits


class TavilySearchClient:
    name = "tavily"
    paid = True  # يُسجل استهلاكه دائمًا؛ السعر 0 في الخطة المجانية

    def __init__(
        self,
        api_key: str,
        *,
        country: str = "SA",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self._country = _TAVILY_COUNTRIES.get(country.upper())
        self._transport = transport

    async def search(self, query: str, *, count: int) -> list[SearchHit]:
        body: dict[str, object] = {
            "query": query[:400],
            "search_depth": "basic",
            "max_results": max(1, min(count, 20)),
            "topic": "general",
            "include_answer": False,
            "include_raw_content": False,
        }
        if self._country:
            body["country"] = self._country
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=30.0) as client:
                resp = await client.post(TAVILY_URL, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise SearchError("تعذر الاتصال بـTavily", retryable=True, code="network") from exc
        if resp.status_code in (401, 403):
            raise SearchError("مفتاح Tavily غير صالح", code="auth")
        if resp.status_code == 429:
            raise SearchError("تجاوز حد طلبات Tavily", retryable=True, code="rate_limit")
        if resp.status_code in (432, 433):
            raise SearchError("نفد رصيد خطة Tavily لهذا الشهر", code="quota")
        if resp.status_code >= 400:
            raise SearchError(
                f"Tavily أعاد خطأ ({resp.status_code})", retryable=resp.status_code >= 500
            )
        try:
            results = resp.json().get("results", [])
        except ValueError as exc:
            raise SearchError("رد غير صالح من Tavily") from exc
        hits = []
        for r in results[:count]:
            url = str(r.get("url", ""))
            if url.startswith(("http://", "https://")):
                hits.append(
                    SearchHit(
                        str(r.get("title", ""))[:300],
                        url[:2000],
                        str(r.get("content", ""))[:500],
                    )
                )
        return hits


class FakeSearchClient:
    name = "fake"
    paid = False

    async def search(self, query: str, *, count: int) -> list[SearchHit]:
        return [
            SearchHit(r["name"], r["website"], r["description"], synthetic=True)
            for r in _FAKE_RESULTS[:count]
        ]


class WebSearchConnector:
    """مصدر «بحث ويب»: العينة استعلام واحد، والاكتشاف استعلامات مخططة ضمن الحدود."""

    key = "web_search"

    def __init__(self, client: SearchClient) -> None:
        self.client = client

    async def validate(self, config: SourceConfig) -> ValidationReport:
        return ValidationReport(
            status="succeeded",
            code="ok",
            message="بحث اصطناعي (FakeSearch)"
            if not self.client.paid
            else f"مزود البحث: {self.client.name}",
        )

    async def sample(self, config: SourceConfig) -> SampleResult:
        query = str(config.extra.get("sample_query") or "شركات خدمات الرياض")
        try:
            hits = await self.client.search(query, count=min(config.max_records, 5))
        except SearchError as exc:
            status = "needs_setup" if exc.code == "auth" else "failed"
            return SampleResult(status=status, code=exc.code, message=str(exc), request_count=1)
        records = [
            SampleRecord(
                source_record_id=h.url,
                url=h.url,
                fields={"name": h.title, "website": h.url, "description": h.description},
            )
            for h in hits
        ]
        synthetic = bool(hits) and all(h.synthetic for h in hits)
        found = sorted({k for r in records for k, v in r.fields.items() if v})
        return SampleResult(
            status="succeeded" if records else "failed",
            code="synthetic" if synthetic else ("ok" if records else "no_records"),
            message=(
                f"عينة اصطناعية من FakeSearch: {len(records)} سجل (ليست بيانات حقيقية)"
                if synthetic
                else f"{len(records)} نتيجة للاستعلام «{query}»"
            ),
            records=records,
            fields_found=found,
            fields_missing=[
                f
                for f in ("name", "description", "website", "phone", "email", "category")
                if f not in found
            ],
            request_count=0 if synthetic else 1,
            cost_amount=None if self.client.paid else Decimal("0"),
            synthetic=synthetic,
        )
