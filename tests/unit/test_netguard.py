from __future__ import annotations

import ipaddress

import pytest

from app.connectors.netguard import (
    BlockedUrl,
    check_url,
    host_allowed,
    ip_is_public,
    resolve_public,
)


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://localhost/", "blocked_private_address"),
        ("http://api.localhost/", "blocked_private_address"),
        ("http://127.0.0.1/", "blocked_private_address"),
        ("http://127.1/", "blocked_private_address"),
        ("http://2130706433/", "blocked_private_address"),
        ("http://0x7f000001/", "blocked_private_address"),
        ("http://0177.0.0.1/", "blocked_private_address"),
        ("http://10.0.0.5/", "blocked_private_address"),
        ("http://192.168.1.1/", "blocked_private_address"),
        ("http://172.16.0.1/", "blocked_private_address"),
        ("http://100.64.0.1/", "blocked_private_address"),
        ("http://169.254.169.254/latest/meta-data", "blocked_private_address"),
        ("http://[::1]/", "blocked_private_address"),
        ("http://[::ffff:127.0.0.1]/", "blocked_private_address"),
        ("http://[fd00:ec2::254]/", "blocked_private_address"),
        ("http://[fe80::1]/", "blocked_private_address"),
        ("http://0.0.0.0/", "blocked_private_address"),
        ("http://metadata.google.internal/", "blocked_private_address"),
        ("http://intranet/", "invalid_url"),
        ("http://user:pass@example.com/", "blocked_userinfo"),
        ("http://example.com@10.0.0.1/", "blocked_userinfo"),
        ("ftp://example.com/", "blocked_scheme"),
        ("file:///etc/passwd", "blocked_scheme"),
        ("javascript:alert(1)", "blocked_scheme"),
        ("http://example.com:22/", "blocked_port"),
        ("http://example.com\\@evil.com/", "invalid_url"),
    ],
)
def test_blocked_urls(url: str, code: str) -> None:
    with pytest.raises(BlockedUrl) as err:
        check_url(url)
    assert err.value.code == code


def test_public_url_normalized() -> None:
    checked = check_url("HTTPS://Example.COM/Path?q=1")
    assert (
        checked.host == "example.com"
        and checked.port == 443
        and checked.url == "https://example.com/Path?q=1"
    )
    assert check_url("https://مثال.السعودية/").host.startswith("xn--")


@pytest.mark.parametrize(
    ("addr", "public"),
    [
        ("93.184.216.34", True),
        ("8.8.8.8", True),
        ("2606:4700:4700::1111", True),
        ("64:ff9b::a00:1", False),  # NAT64 يغلف 10.0.0.1
        ("2002:a00:1::", False),  # 6to4 يغلف 10.0.0.1
        ("224.0.0.1", False),
        ("::ffff:10.0.0.1", False),
    ],
)
def test_ip_public(addr: str, public: bool) -> None:
    assert ip_is_public(ipaddress.ip_address(addr)) is public


async def test_resolve_rejects_any_private_record() -> None:
    async def resolver(host: str, port: int) -> list[str]:
        return ["93.184.216.34", "10.0.0.7"]

    with pytest.raises(BlockedUrl) as err:
        await resolve_public("mixed.example", 443, resolver)
    assert err.value.code == "blocked_private_address"


def test_host_allowlist_semantics() -> None:
    assert host_allowed("a.example.com", ["*.example.com"])
    assert not host_allowed("example.com", ["*.example.com"])
    assert not host_allowed("evilexample.com", ["*.example.com"])
    assert host_allowed("example.com", ["example.com"])
    assert not host_allowed("sub.example.com", ["example.com"])
