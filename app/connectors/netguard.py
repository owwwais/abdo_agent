"""حماية جلب الروابط (SSRF): فحص الرابط قبل أي اتصال، وفحص كل عنوان IP محلول،
والاتصال بالعنوان الذي فُحص نفسه (منع DNS rebinding)، وإعادة الفحص عند كل تحويل."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]

_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".lan",
    ".home.arpa",
    ".corp",
)
_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata"}
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")


class BlockedUrl(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class CheckedUrl:
    scheme: str
    host: str  # hostname بصيغة ASCII/IDNA وبأحرف صغيرة، أو IP
    port: int
    path: str
    query: str
    is_ip_literal: bool

    @property
    def netloc(self) -> str:
        default = 443 if self.scheme == "https" else 80
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == default else f"{host}:{self.port}"

    @property
    def url(self) -> str:
        q = f"?{self.query}" if self.query else ""
        return f"{self.scheme}://{self.netloc}{self.path or '/'}{q}"


def ip_is_public(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip_is_public(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return ip_is_public(ip.sixtofour)
        if ip.teredo is not None:
            return False
        if ip in _NAT64:
            return ip_is_public(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return bool(ip.is_global) and not ip.is_multicast and not ip.is_reserved


def _parse_ip(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    # صيغ IPv4 غير القياسية (مثل 2130706433 أو 0x7f.1) تعاملها بعض المكتبات كعناوين.
    if re.fullmatch(r"[0-9x.]+", host) or re.fullmatch(r"0x[0-9a-f]+", host):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return None
    return None


def check_url(url: str, *, allowed_ports: list[int] | tuple[int, ...] = (80, 443)) -> CheckedUrl:
    """فحص ثابت قبل أي اتصال. يرفع BlockedUrl برسالة عربية."""
    url = (url or "").strip()
    if not url or len(url) > 2000:
        raise BlockedUrl("invalid_url", "الرابط فارغ أو طويل جدًا")
    if any(ch in url for ch in ("\\", "\n", "\r", "\t", " ")):
        raise BlockedUrl("invalid_url", "الرابط يحتوي أحرفًا غير مسموحة")
    try:
        parts: SplitResult = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise BlockedUrl("invalid_url", "صيغة الرابط غير صالحة") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise BlockedUrl("blocked_scheme", "يُسمح فقط بروابط http وhttps")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise BlockedUrl("blocked_userinfo", "لا يُسمح ببيانات دخول داخل الرابط")
    raw_host = (parts.hostname or "").rstrip(".").lower()
    if not raw_host:
        raise BlockedUrl("invalid_url", "الرابط بلا اسم نطاق")
    port = port or (443 if scheme == "https" else 80)
    if port not in allowed_ports:
        raise BlockedUrl("blocked_port", f"المنفذ {port} غير مسموح")
    ip = _parse_ip(raw_host)
    if ip is not None:
        if not ip_is_public(ip):
            raise BlockedUrl("blocked_private_address", "الرابط يشير إلى عنوان داخلي أو محجوز")
        host = str(ip)
    else:
        try:
            host = raw_host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise BlockedUrl("invalid_url", "اسم النطاق غير صالح") from exc
        if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_HOST_SUFFIXES):
            raise BlockedUrl("blocked_private_address", "الرابط يشير إلى اسم داخلي")
        if not _HOST_RE.match(host):
            raise BlockedUrl("invalid_url", "اسم النطاق غير صالح أو ليس اسمًا عامًا")
    return CheckedUrl(scheme, host, port, parts.path, parts.query, ip is not None)


def host_allowed(host: str, allowed_hosts: list[str] | tuple[str, ...]) -> bool:
    """مطابقة تامة، أو *.example.com لأي نطاق فرعي (لا يشمل example.com نفسه)."""
    for pattern in allowed_hosts:
        pattern = pattern.lower().strip()
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]):
                return True
        elif host == pattern:
            return True
    return False


def path_allowed(path: str, allowed_paths: list[str] | tuple[str, ...]) -> bool:
    if not allowed_paths:
        return True
    path = path or "/"
    return any(path.startswith(prefix) for prefix in allowed_paths)


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


async def resolve_public(host: str, port: int, resolver: Resolver) -> str:
    """يحل الاسم ويرفض إن كان أي عنوان غير عام، ويعيد العنوان الذي سيُتصل به فعلًا."""
    ip = _parse_ip(host)
    if ip is not None:
        if not ip_is_public(ip):
            raise BlockedUrl("blocked_private_address", "الرابط يشير إلى عنوان داخلي أو محجوز")
        return str(ip)
    try:
        addresses = await resolver(host, port)
    except OSError as exc:
        raise BlockedUrl("dns_failed", "تعذر حل اسم النطاق") from exc
    if not addresses:
        raise BlockedUrl("dns_failed", "تعذر حل اسم النطاق")
    for addr in addresses:
        parsed = ipaddress.ip_address(addr.split("%")[0])
        if not ip_is_public(parsed):
            raise BlockedUrl(
                "blocked_private_address",
                "اسم النطاق يُحل إلى عنوان داخلي أو محجوز؛ رُفض قبل الاتصال",
            )
    return addresses[0].split("%")[0]
