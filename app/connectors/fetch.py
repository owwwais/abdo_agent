"""جالب HTTP محدود: عنوان IP مفحوص، تحويلات يدوية مفحوصة، حدود للحجم بعد فك الضغط والوقت والعدد
ونوع المحتوى. لا ينفذ JavaScript ولا يقبل ملفات تنفيذية."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlunsplit

import httpx

from app.connectors.netguard import (
    BlockedUrl,
    Resolver,
    check_url,
    host_allowed,
    path_allowed,
    resolve_public,
    system_resolver,
)

TEXT_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "application/rss+xml",
        "application/atom+xml",
        "application/xml",
        "text/xml",
    }
)


class FetchError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int | None = None) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


@dataclass
class FetchLimits:
    max_requests: int = 6
    max_bytes: int = 2_000_000
    max_redirects: int = 3
    connect_timeout: float = 5.0
    read_timeout: float = 10.0


@dataclass
class FetchResult:
    url: str
    status_code: int
    content_type: str
    text: str
    size: int
    redirects: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class SafeFetcher:
    def __init__(
        self,
        *,
        user_agent: str,
        allowed_hosts: list[str],
        allowed_paths: list[str] | None = None,
        allowed_ports: list[int] | None = None,
        limits: FetchLimits | None = None,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.allowed_hosts = allowed_hosts
        self.allowed_paths = allowed_paths or []
        self.allowed_ports = allowed_ports or [80, 443]
        self.limits = limits or FetchLimits()
        self._resolver = resolver or system_resolver
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,  # البروكسي من البيئة قد يتجاوز تثبيت العنوان المفحوص
            timeout=httpx.Timeout(self.limits.read_timeout, connect=self.limits.connect_timeout),
            limits=httpx.Limits(max_keepalive_connections=0, max_connections=4),
        )
        self.request_count = 0
        self.bytes_total = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SafeFetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def get(
        self,
        url: str,
        *,
        accept: frozenset[str] = TEXT_TYPES,
        enforce_paths: bool = True,
    ) -> FetchResult:
        started = time.monotonic()
        redirects: list[str] = []
        current = url
        for _hop in range(self.limits.max_redirects + 1):
            try:
                target = check_url(current, allowed_ports=self.allowed_ports)
            except BlockedUrl as exc:
                raise FetchError(exc.code, exc.message) from exc
            if not host_allowed(target.host, self.allowed_hosts):
                raise FetchError(
                    "host_not_allowed", f"النطاق {target.host} خارج النطاقات المسموحة للمصدر"
                )
            if enforce_paths and not path_allowed(target.path, self.allowed_paths):
                raise FetchError("path_not_allowed", "المسار خارج المسارات المسموحة للمصدر")
            try:
                ip = await resolve_public(target.host, target.port, self._resolver)
            except BlockedUrl as exc:
                raise FetchError(exc.code, exc.message) from exc
            if self.request_count >= self.limits.max_requests:
                raise FetchError("request_limit", "بلغ الفحص حد الطلبات المسموح")
            self.request_count += 1

            ip_host = f"[{ip}]" if ":" in ip else ip
            wire_url = urlunsplit(
                (target.scheme, f"{ip_host}:{target.port}", target.path or "/", target.query, "")
            )
            headers = {
                "Host": target.netloc,
                "User-Agent": self.user_agent,
                "Accept": ", ".join(sorted(accept)) + ";q=0.9, */*;q=0.1",
                "Accept-Language": "ar,en;q=0.8",
            }
            extensions = {"sni_hostname": target.host} if target.scheme == "https" else {}
            try:
                async with self._client.stream(
                    "GET", wire_url, headers=headers, extensions=extensions
                ) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            raise FetchError("bad_redirect", "تحويل بلا وجهة")
                        current = urljoin(target.url, location)
                        redirects.append(current)
                        continue
                    content_type = (
                        resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    )
                    if not (200 <= resp.status_code < 300):
                        return FetchResult(
                            target.url,
                            resp.status_code,
                            content_type,
                            "",
                            0,
                            redirects,
                            int((time.monotonic() - started) * 1000),
                        )
                    if content_type not in accept:
                        raise FetchError(
                            "bad_content_type",
                            f"نوع المحتوى {content_type or 'غير معروف'} غير مسموح",
                            status_code=resp.status_code,
                        )
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self.limits.max_bytes:
                        raise FetchError("too_large", "حجم الصفحة يتجاوز الحد المسموح")
                    chunks: list[bytes] = []
                    size = 0
                    # iter_bytes يعيد المحتوى بعد فك الضغط؛ الحد يطبق على الحجم الفعلي.
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > self.limits.max_bytes:
                            raise FetchError("too_large", "حجم الصفحة بعد فك الضغط يتجاوز الحد")
                        chunks.append(chunk)
                    self.bytes_total += size
                    body = b"".join(chunks)
                    encoding = resp.charset_encoding or "utf-8"
                    try:
                        text = body.decode(encoding, errors="replace")
                    except LookupError:
                        text = body.decode("utf-8", errors="replace")
                    return FetchResult(
                        target.url,
                        resp.status_code,
                        content_type,
                        text,
                        size,
                        redirects,
                        int((time.monotonic() - started) * 1000),
                    )
            except httpx.TimeoutException as exc:
                raise FetchError("timeout", "انتهت مهلة الاتصال بالمصدر") from exc
            except httpx.HTTPError as exc:
                raise FetchError("network_error", "تعذر الاتصال بالمصدر") from exc
        raise FetchError("too_many_redirects", "عدد التحويلات تجاوز الحد")
