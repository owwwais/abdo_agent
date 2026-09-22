"""ربط نوع المصدر بمفتاح الموصل ووضع الوصول، وإنشاء الموصل المناسب."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import Settings
from app.connectors.base import SourceConnector
from app.connectors.netguard import Resolver
from app.connectors.search import SearchClient, WebSearchConnector
from app.connectors.simple import ManualConnector, UnconfiguredConnector
from app.connectors.web import HtmlConnector, RssConnector


@dataclass(frozen=True)
class KindSpec:
    label: str
    access_mode: str
    needs_url: bool
    fetches: bool  # هل يتصل بالشبكة
    help: str


SOURCE_KINDS: dict[str, KindSpec] = {
    "manual": KindSpec(
        "مقابلات وإحالات", "manual_only", False, False, "إدخال يدوي واستيراد CSV مع معاينة"
    ),
    "html": KindSpec(
        "موقع منشأة أو دليل HTML عام",
        "public_web",
        True,
        True,
        "قارئ محدود لنطاق مصرح به؛ حدد المسارات المسموحة لصفحات الدليل",
    ),
    "rss": KindSpec("تغذية RSS/Atom", "public_web", True, True, "تغذية عامة مسموح استخدامها"),
    "portal": KindSpec(
        "بوابة فرص (مثل منصة «فرصة»)",
        "public_web",
        True,
        True,
        "تحليل المحتوى العام المسموح فقط؛ الوصول الخاص يحتاج موصلًا مخصصًا لاحقًا",
    ),
    "web_search": KindSpec(
        "بحث ويب", "api_key", False, True, "يحتاج مزود بحث وحسابًا وشروط استخدام محددة"
    ),
    "linkedin": KindSpec(
        "LinkedIn (يدوي)",
        "manual_only",
        False,
        False,
        "إدخال ومراجعة بشرية؛ لا تسجيل دخول آلي ولا scraping",
    ),
    "google_maps": KindSpec(
        "Google Maps",
        "api_key",
        False,
        True,
        "غير مفعل افتراضيًا؛ يحتاج تحديد الاستخدام المسموح وسياسات البيانات",
    ),
}


def connector_key_for(kind: str, settings: Settings) -> str:
    if kind in ("manual", "linkedin"):
        return "manual"
    if kind in ("html", "portal"):
        return "html"
    if kind == "rss":
        return "rss"
    if kind == "web_search":
        # المزود الفعلي (اصطناعي أو Brave) يُحدد وقت التشغيل من صفحة الإعدادات.
        return "web_search"
    return kind


def get_connector(
    key: str,
    settings: Settings,
    *,
    resolver: Resolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    search: SearchClient | None = None,
) -> SourceConnector:
    if key == "manual":
        return ManualConnector()
    if key == "html":
        return HtmlConnector(settings, resolver=resolver, transport=transport)
    if key == "rss":
        return RssConnector(settings, resolver=resolver, transport=transport)
    if key in ("web_search", "fake_search"):
        if search is None:
            return UnconfiguredConnector(
                key, "البحث على الويب غير مهيأ", "اختر المزود وأدخل مفتاحه في الإعدادات ← البحث"
            )
        return WebSearchConnector(search)
    if key == "google_maps":
        return UnconfiguredConnector(
            key,
            "Google Maps غير مفعل افتراضيًا",
            "حدد الاستخدام المسموح وفق سياسات Google Places ثم أضف موصلًا مخصصًا",
        )
    return UnconfiguredConnector(key, "موصل غير معروف", "راجع إعداد المصدر")
