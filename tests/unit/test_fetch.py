"""SafeFetcher: التحقق عند كل تحويل وعند الاتصال الفعلي (T04)، وحدود الحجم والنوع والعدد."""

from __future__ import annotations

import gzip

import httpx
import pytest

from app.connectors.fetch import FetchError, FetchLimits, SafeFetcher
from tests.fakes import PUBLIC_IP, FakeNet


def fetcher(net: FakeNet, hosts: list[str], **limits: int) -> SafeFetcher:
    return SafeFetcher(
        user_agent="TestAgent/1",
        allowed_hosts=hosts,
        limits=FetchLimits(**limits),
        resolver=net.resolve,
        transport=net.transport,
    )


async def test_connects_to_validated_ip_with_original_host_header() -> None:
    net = FakeNet()
    net.add_site("site.example")
    net.html("site.example", "/", "<h1>ok</h1>")
    async with fetcher(net, ["site.example"]) as f:
        res = await f.get("https://site.example/")
    assert res.ok and "ok" in res.text
    assert net.seen == [(PUBLIC_IP, "site.example", "/")]


async def test_redirect_to_private_ip_rejected_before_request() -> None:
    net = FakeNet()
    net.add_site("site.example")
    net.route(
        "site.example",
        "/",
        httpx.Response(302, headers={"location": "http://169.254.169.254/latest"}),
    )
    async with fetcher(net, ["site.example", "169.254.169.254"]) as f:
        with pytest.raises(FetchError) as err:
            await f.get("https://site.example/")
    assert err.value.code == "blocked_private_address"
    assert [s[0] for s in net.seen] == [PUBLIC_IP]  # لم يصل أي طلب للعنوان الداخلي


async def test_redirect_to_host_resolving_private_rejected() -> None:
    net = FakeNet()
    net.add_site("site.example")
    net.add_site("rebind.example", "10.0.0.5")
    net.route(
        "site.example",
        "/",
        httpx.Response(301, headers={"location": "https://rebind.example/admin"}),
    )
    async with fetcher(net, ["site.example", "rebind.example"]) as f:
        with pytest.raises(FetchError) as err:
            await f.get("https://site.example/")
    assert err.value.code == "blocked_private_address"
    assert all(s[1] != "rebind.example" for s in net.seen)


async def test_redirect_outside_allowed_hosts_rejected() -> None:
    net = FakeNet()
    net.add_site("site.example")
    net.add_site("other.example")
    net.route(
        "site.example", "/", httpx.Response(302, headers={"location": "https://other.example/"})
    )
    async with fetcher(net, ["site.example"]) as f:
        with pytest.raises(FetchError) as err:
            await f.get("https://site.example/")
    assert err.value.code == "host_not_allowed"


async def test_size_limit_applies_after_decompression() -> None:
    net = FakeNet()
    net.add_site("site.example")
    body = gzip.compress(b"a" * 50_000)
    net.route(
        "site.example",
        "/",
        httpx.Response(
            200, headers={"content-type": "text/html", "content-encoding": "gzip"}, content=body
        ),
    )
    async with fetcher(net, ["site.example"], max_bytes=10_000) as f:
        with pytest.raises(FetchError) as err:
            await f.get("https://site.example/")
    assert err.value.code == "too_large"
    assert len(body) < 10_000  # المضغوط صغير؛ الحد يطبق على الحجم الفعلي


async def test_binary_content_type_rejected() -> None:
    net = FakeNet()
    net.add_site("site.example")
    net.route(
        "site.example",
        "/a.exe",
        httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"MZ"),
    )
    async with fetcher(net, ["site.example"]) as f:
        with pytest.raises(FetchError) as err:
            await f.get("https://site.example/a.exe")
    assert err.value.code == "bad_content_type"


async def test_request_limit_and_redirect_limit() -> None:
    net = FakeNet()
    net.add_site("site.example")
    net.route("site.example", "/loop", httpx.Response(302, headers={"location": "/loop"}))
    async with fetcher(net, ["site.example"], max_requests=10, max_redirects=3) as f:
        with pytest.raises(FetchError) as err:
            await f.get("https://site.example/loop")
    assert err.value.code == "too_many_redirects"
    async with fetcher(net, ["site.example"], max_requests=2, max_redirects=5) as f:
        with pytest.raises(FetchError) as err:
            await f.get("https://site.example/loop")
    assert err.value.code == "request_limit"
