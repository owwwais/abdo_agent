"""موصلات الويب العامة: صفحة/دليل HTML وتغذية RSS/Atom، ضمن نطاق مصرح به وحدود صغيرة."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from defusedxml import ElementTree as SafeET  # يمنع هجمات XML (entity expansion وغيرها)

from app.config import Settings
from app.connectors.base import (
    EXPECTED_FIELDS,
    ConnectorIssue,
    SampleRecord,
    SampleResult,
    SourceConfig,
    ValidationReport,
)
from app.connectors.fetch import TEXT_TYPES, FetchError, FetchLimits, FetchResult, SafeFetcher
from app.connectors.htmlparse import ParsedPage, clean, parse_html, strip_html
from app.connectors.netguard import BlockedUrl, Resolver, check_url, host_allowed, path_allowed

_CONTACT_HINTS = (
    "contact",
    "about",
    "تواصل",
    "اتصل",
    "من-نحن",
    "عن-",
    "%d8%aa%d9%88%d8%a7%d8%b5%d9%84",
)
_FEED_TYPES = frozenset(
    {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml"}
)
_ATOM = "{http://www.w3.org/2005/Atom}"
_ALLOW_ALL = ["User-agent: *", "Allow: /"]


def _status_from_http(res: FetchResult) -> tuple[str, str, str] | None:
    """يحول حالة HTTP غير الناجحة إلى (status, code, message)."""
    code = res.status_code
    if code in (401, 403, 407):
        return (
            "restricted",
            "access_denied",
            f"المصدر يرفض الوصول (HTTP {code}). لا نحاول تجاوز الحماية؛ يلزم وصول مصرح أو إدخال يدوي",
        )
    if code == 429:
        return ("failed", "rate_limited", "المصدر يطلب الإبطاء (HTTP 429). أعد المحاولة لاحقًا")
    if code in (404, 410):
        return ("failed", "not_found", f"الصفحة غير موجودة (HTTP {code})")
    if code >= 400:
        return ("failed", "http_error", f"استجابة غير ناجحة من المصدر (HTTP {code})")
    return None


def _error_result(exc: FetchError, fetcher: SafeFetcher | None, pages: int = 0) -> SampleResult:
    restricted_codes = {
        "blocked_private_address",
        "blocked_scheme",
        "blocked_userinfo",
        "blocked_port",
        "host_not_allowed",
        "path_not_allowed",
    }
    status = "restricted" if exc.code in restricted_codes else "failed"
    return SampleResult(
        status=status,
        code=exc.code,
        message=exc.message,
        issues=[ConnectorIssue(code=exc.code, message=exc.message)],
        pages_fetched=pages,
        request_count=fetcher.request_count if fetcher else 0,
        bytes_fetched=fetcher.bytes_total if fetcher else 0,
        cost_amount=Decimal("0"),
    )


class _WebBase:
    key = "web"

    def __init__(
        self,
        settings: Settings,
        *,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._resolver = resolver
        self._transport = transport

    @property
    def _ua_token(self) -> str:
        return self.settings.fetch_user_agent.split("/")[0].strip() or "*"

    async def validate(self, config: SourceConfig) -> ValidationReport:
        if not self.settings.source_fetch_enabled:
            return ValidationReport(
                status="needs_setup",
                code="fetch_disabled",
                message="الجلب الشبكي معطل في هذه البيئة",
                next_step="فعّل SOURCE_FETCH_ENABLED=true في بيئة development أو staging",
            )
        if not config.url:
            return ValidationReport(status="failed", code="missing_url", message="المصدر بلا رابط")
        try:
            target = check_url(config.url, allowed_ports=self.settings.fetch_allowed_ports)
        except BlockedUrl as exc:
            return ValidationReport(status="restricted", code=exc.code, message=exc.message)
        if not host_allowed(target.host, list(config.allowed_hosts)):
            return ValidationReport(
                status="restricted",
                code="host_not_allowed",
                message="نطاق الرابط ليس ضمن النطاقات المسموحة للمصدر",
            )
        if not path_allowed(target.path, list(config.allowed_paths)):
            return ValidationReport(
                status="restricted",
                code="path_not_allowed",
                message="مسار الرابط ليس ضمن المسارات المسموحة للمصدر",
            )
        return ValidationReport(status="succeeded", code="ok", message="الإعداد صالح للفحص")

    def _fetcher(self, config: SourceConfig) -> SafeFetcher:
        return SafeFetcher(
            user_agent=self.settings.fetch_user_agent,
            allowed_hosts=list(config.allowed_hosts),
            allowed_paths=list(config.allowed_paths),
            allowed_ports=self.settings.fetch_allowed_ports,
            limits=FetchLimits(max_requests=config.max_requests),
            resolver=self._resolver,
            transport=self._transport,
        )

    async def _robots(self, fetcher: SafeFetcher, url: str) -> RobotFileParser | SampleResult:
        """RFC 9309: 4xx = لا قيود، 5xx أو تعذر الوصول = منع كامل. robots وحده ليس ترخيصًا."""
        parts = urlsplit(url)
        rp = RobotFileParser()
        try:
            res = await fetcher.get(
                f"{parts.scheme}://{parts.netloc}/robots.txt",
                accept=frozenset({"text/plain"}) | TEXT_TYPES,
                enforce_paths=False,
            )
        except FetchError as exc:
            if exc.code == "bad_content_type":
                rp.parse(_ALLOW_ALL)
                return rp
            return _error_result(exc, fetcher)
        if res.ok and res.content_type == "text/plain":
            rp.parse(res.text.splitlines())
        elif res.ok or 400 <= res.status_code < 500:
            rp.parse(_ALLOW_ALL)
        else:
            return SampleResult(
                status="restricted",
                code="robots_unavailable",
                message="تعذر قراءة robots.txt (خطأ خادم)؛ يُفترض المنع حتى يتاح",
                request_count=fetcher.request_count,
                cost_amount=Decimal("0"),
            )
        return rp

    def _finish(
        self,
        records: list[SampleRecord],
        fetcher: SafeFetcher,
        pages: int,
        issues: list[ConnectorIssue],
        expected: tuple[str, ...],
        config: SourceConfig,
    ) -> SampleResult:
        found = sorted({k for r in records for k, v in r.fields.items() if v})
        missing = [f for f in expected if f not in found]
        if not records:
            return SampleResult(
                status="failed",
                code="no_records",
                message="لم تُستخرج أي سجلات من العينة",
                next_step="راجع الرابط أو المسارات المسموحة",
                issues=issues,
                pages_fetched=pages,
                request_count=fetcher.request_count,
                bytes_fetched=fetcher.bytes_total,
                cost_amount=Decimal("0"),
            )
        return SampleResult(
            status="succeeded",
            code="ok",
            message=f"استُخرج {len(records)} سجل من {pages} صفحة",
            records=records[: config.max_records],
            fields_found=found,
            fields_missing=missing,
            issues=issues,
            pages_fetched=pages,
            request_count=fetcher.request_count,
            bytes_fetched=fetcher.bytes_total,
            cost_amount=Decimal("0"),
            cost_currency="USD",
            retention_note=(
                "تُحفظ الحقول المستخرجة فقط" if not config.store_raw else "مسموح حفظ المحتوى الخام"
            ),
        )


class HtmlConnector(_WebBase):
    key = "html"

    async def sample(self, config: SourceConfig) -> SampleResult:
        report = await self.validate(config)
        if report.status != "succeeded":
            return SampleResult(**report.model_dump())
        assert config.url
        fetcher = self._fetcher(config)
        pages = 0
        issues: list[ConnectorIssue] = []
        records: list[SampleRecord] = []
        try:
            async with fetcher, asyncio.timeout(config.timeout_seconds):
                robots = await self._robots(fetcher, config.url)
                if isinstance(robots, SampleResult):
                    return robots
                if not robots.can_fetch(self._ua_token, config.url):
                    return SampleResult(
                        status="restricted",
                        code="robots_disallow",
                        message="robots.txt يمنع هذا المسار",
                        request_count=fetcher.request_count,
                        cost_amount=Decimal("0"),
                    )
                res = await fetcher.get(
                    config.url, accept=frozenset({"text/html", "application/xhtml+xml"})
                )
                pages += 1
                bad = _status_from_http(res)
                if bad:
                    status, code, message = bad
                    return SampleResult(
                        status=status,
                        code=code,
                        message=message,
                        request_count=fetcher.request_count,
                        pages_fetched=pages,
                        cost_amount=Decimal("0"),
                    )
                start = parse_html(res.text, res.url)
                if start.login_wall:
                    return SampleResult(
                        status="restricted",
                        code="login_required",
                        message="الصفحة تطلب تسجيل الدخول؛ لا يُسجَّل الدخول آليًا",
                        next_step="استخدم وصولًا رسميًا مصرحًا أو الإدخال اليدوي",
                        request_count=fetcher.request_count,
                        pages_fetched=pages,
                        cost_amount=Decimal("0"),
                    )
                candidates = self._detail_links(start.links, config)
                if not config.allowed_paths:
                    records.append(self._record(start))
                for link in candidates[: max(0, config.max_pages - 1)]:
                    if not robots.can_fetch(self._ua_token, link):
                        issues.append(
                            ConnectorIssue(
                                code="robots_disallow", message="مسار ممنوع في robots.txt", url=link
                            )
                        )
                        continue
                    try:
                        detail = await fetcher.get(
                            link, accept=frozenset({"text/html", "application/xhtml+xml"})
                        )
                    except FetchError as exc:
                        issues.append(ConnectorIssue(code=exc.code, message=exc.message, url=link))
                        if exc.code == "request_limit":
                            break
                        continue
                    pages += 1
                    if not detail.ok:
                        issues.append(
                            ConnectorIssue(
                                code="http_error", message=f"HTTP {detail.status_code}", url=link
                            )
                        )
                        continue
                    parsed = parse_html(detail.text, detail.url)
                    if config.allowed_paths:
                        records.append(self._record(parsed))
                    else:
                        # موقع منشأة واحدة: صفحات التواصل تكمل حقول السجل نفسه.
                        base = records[0]
                        for key in ("email", "phone"):
                            if key not in base.fields and parsed.fields.get(key):
                                base.fields[key] = parsed.fields[key]
                if config.allowed_paths and not records:
                    records.append(self._record(start))
        except FetchError as exc:
            return _error_result(exc, fetcher, pages)
        except TimeoutError:
            issues.append(ConnectorIssue(code="timeout", message="انتهت مهلة الفحص الكلية"))
        return self._finish(records, fetcher, pages, issues, EXPECTED_FIELDS, config)

    @staticmethod
    def _record(page: ParsedPage) -> SampleRecord:
        return SampleRecord(
            source_record_id=page.url,
            url=page.url,
            fields=dict(page.fields),
            links=page.links[:10],
            excerpt=page.fields.get("description"),
        )

    @staticmethod
    def _detail_links(links: list[str], config: SourceConfig) -> list[str]:
        allowed = [
            link
            for link in links
            if host_allowed(urlsplit(link).hostname or "", list(config.allowed_hosts))
        ]
        if config.allowed_paths:
            return [
                link
                for link in allowed
                if path_allowed(urlsplit(link).path, list(config.allowed_paths))
                and link.rstrip("/") != (config.url or "").rstrip("/")
            ]
        return [link for link in allowed if any(h in link.lower() for h in _CONTACT_HINTS)]


class RssConnector(_WebBase):
    key = "rss"

    async def sample(self, config: SourceConfig) -> SampleResult:
        report = await self.validate(config)
        if report.status != "succeeded":
            return SampleResult(**report.model_dump())
        assert config.url
        fetcher = self._fetcher(config)
        try:
            async with fetcher, asyncio.timeout(config.timeout_seconds):
                robots = await self._robots(fetcher, config.url)
                if isinstance(robots, SampleResult):
                    return robots
                if not robots.can_fetch(self._ua_token, config.url):
                    return SampleResult(
                        status="restricted",
                        code="robots_disallow",
                        message="robots.txt يمنع هذا المسار",
                        request_count=fetcher.request_count,
                        cost_amount=Decimal("0"),
                    )
                res = await fetcher.get(config.url, accept=_FEED_TYPES | {"text/html"})
        except FetchError as exc:
            return _error_result(exc, fetcher)
        except TimeoutError:
            return SampleResult(
                status="failed",
                code="timeout",
                message="انتهت مهلة الفحص الكلية",
                request_count=fetcher.request_count,
                cost_amount=Decimal("0"),
            )
        bad = _status_from_http(res)
        if bad:
            status, code, message = bad
            return SampleResult(
                status=status,
                code=code,
                message=message,
                request_count=fetcher.request_count,
                pages_fetched=1,
                cost_amount=Decimal("0"),
            )
        if res.content_type == "text/html":
            return SampleResult(
                status="failed",
                code="not_a_feed",
                message="الرابط صفحة HTML وليس تغذية RSS/Atom",
                next_step="استخدم نوع مصدر «صفحة/دليل HTML» أو رابط التغذية الصحيح",
                request_count=fetcher.request_count,
                pages_fetched=1,
                cost_amount=Decimal("0"),
            )
        try:
            root = SafeET.fromstring(res.text.encode("utf-8"))
        except Exception:
            return SampleResult(
                status="failed",
                code="invalid_feed",
                message="تعذر تحليل التغذية",
                request_count=fetcher.request_count,
                pages_fetched=1,
                cost_amount=Decimal("0"),
            )
        records = [r for r in self._items(root)][: config.max_records]
        return self._finish(
            records, fetcher, 1, [], ("name", "link", "published_at", "description"), config
        )

    @staticmethod
    def _items(root: object) -> list[SampleRecord]:
        out: list[SampleRecord] = []

        def text(el: object, tag: str) -> str:
            node = el.find(tag)  # type: ignore[attr-defined]
            return (node.text or "").strip() if node is not None and node.text else ""

        items = root.findall("./channel/item")  # type: ignore[attr-defined]
        for item in items:
            link = text(item, "link")
            out.append(
                SampleRecord(
                    source_record_id=text(item, "guid") or link or text(item, "title"),
                    url=link or None,
                    fields={
                        k: v
                        for k, v in {
                            "name": clean(text(item, "title"), 200),
                            "link": link,
                            "published_at": text(item, "pubDate"),
                            "description": clean(strip_html(text(item, "description")), 300),
                            "category": text(item, "category"),
                        }.items()
                        if v
                    },
                )
            )
        for entry in root.findall(f"{_ATOM}entry"):  # type: ignore[attr-defined]
            link_el = entry.find(f"{_ATOM}link")
            link = link_el.get("href", "") if link_el is not None else ""
            cat_el = entry.find(f"{_ATOM}category")
            out.append(
                SampleRecord(
                    source_record_id=text(entry, f"{_ATOM}id") or link,
                    url=link or None,
                    fields={
                        k: v
                        for k, v in {
                            "name": clean(text(entry, f"{_ATOM}title"), 200),
                            "link": link,
                            "published_at": text(entry, f"{_ATOM}updated")
                            or text(entry, f"{_ATOM}published"),
                            "description": clean(strip_html(text(entry, f"{_ATOM}summary")), 300),
                            "category": cat_el.get("term", "") if cat_el is not None else "",
                        }.items()
                        if v
                    },
                )
            )
        return out
