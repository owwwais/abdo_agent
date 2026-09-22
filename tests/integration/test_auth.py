"""M0/F-002: الزائر لا يصل للبيانات، CSRF، الأدوار، fixture الدخول المحلي، وإبطال الجلسات."""

from __future__ import annotations

import re

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.providers import DEV_USERS
from app.db.models import Membership, UserProfile, Workspace
from app.main import create_app
from tests.conftest import ClientFactory, WorkspaceFixture, make_settings

PAGES = [
    "/",
    "/products",
    "/products/new",
    "/segments",
    "/sources",
    "/sources/new",
    "/companies",
    "/imports",
    "/settings",
]
API = ["/api/me", "/api/products", "/api/segments", "/api/sources", "/api/companies"]


async def test_visitor_cannot_reach_data(client_for: ClientFactory, ws: WorkspaceFixture) -> None:
    client = await client_for(None)
    for path in PAGES:
        r = await client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    for path in API:
        r = await client.get(path)
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated", path
    r = await client.post("/api/products", json={"name": "x"})
    assert r.status_code == 401


async def test_forged_cookie_rejected(client_for: ClientFactory) -> None:
    client = await client_for(None)
    client.cookies.set("sa_session", "forged-token-value")
    assert (await client.get("/api/me")).status_code == 401


async def test_csrf_required_for_mutations(client_for: ClientFactory, ws: WorkspaceFixture) -> None:
    client = await client_for(ws.owner, csrf=False)
    r = await client.post("/api/segments", json={"name": "فئة"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    me = (await client.get("/api/me")).json()
    r = await client.post(
        "/api/segments",
        json={"name": "فئة"},
        headers={"X-CSRF-Token": me["csrf_token"], "Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    r = await client.post(
        "/api/segments", json={"name": "فئة"}, headers={"X-CSRF-Token": me["csrf_token"]}
    )
    assert r.status_code == 201


async def test_reviewer_cannot_change_configuration(
    client_for: ClientFactory, ws: WorkspaceFixture
) -> None:
    reviewer = await client_for(ws.reviewer)
    assert (await reviewer.get("/api/products")).status_code == 200
    r = await reviewer.post("/api/products", json={"name": "منتج"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "owner_only"
    r = await reviewer.post("/api/sources", json={"name": "مصدر", "kind": "manual"})
    assert r.status_code == 403


async def test_disabled_membership_invalidates_session(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    client = await client_for(ws.owner)
    assert (await client.get("/api/me")).status_code == 200
    async with sm() as db, db.begin():
        await db.execute(
            update(Membership)
            .where(Membership.auth_user_id == ws.owner.auth_user_id)
            .values(status="disabled")
        )
    assert (await client.get("/api/me")).status_code == 401


async def test_logout_revokes_session(client_for: ClientFactory, ws: WorkspaceFixture) -> None:
    client = await client_for(ws.owner)
    token = client.cookies.get("sa_session")
    csrf = client.headers["X-CSRF-Token"]
    r = await client.post("/logout", data={"csrf_token": csrf})
    assert r.status_code == 303
    client.cookies.set("sa_session", token)
    assert (await client.get("/api/me")).status_code == 401


async def _seed_dev_users(sm: async_sessionmaker[AsyncSession]) -> None:
    async with sm() as db, db.begin():
        workspace = Workspace(name="dev")
        db.add(workspace)
        await db.flush()
        for user in DEV_USERS.values():
            db.add(
                UserProfile(
                    auth_user_id=user["auth_user_id"],
                    email=user["email"],
                    display_name=user["display_name"],
                    is_dev_fixture=True,
                )
            )
            await db.flush()
            db.add(
                Membership(
                    workspace_id=workspace.id, auth_user_id=user["auth_user_id"], role=user["role"]
                )
            )


async def _dev_login(client: httpx.AsyncClient, user: str = "owner") -> httpx.Response:
    page = await client.get("/login")
    match = re.search(r'name="login_csrf" value="([^"]+)"', page.text)
    token = match.group(1) if match else "missing"
    return await client.post("/auth/dev-login", data={"login_csrf": token, "user": user})


async def test_dev_login_local_only(
    client_for: ClientFactory, sm: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_dev_users(sm)
    local = await client_for(None)
    r = await _dev_login(local)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert (await local.get("/api/me")).json()["role"] == "owner"

    remote = await client_for(None, client_host="203.0.113.9")
    page = await remote.get("/login")
    assert "دخول التطوير المحلي" not in page.text
    r = await remote.post("/auth/dev-login", data={"login_csrf": "x", "user": "owner"})
    assert r.status_code == 400 and "sa_session" not in remote.cookies


async def test_dev_login_requires_login_csrf(
    client_for: ClientFactory, sm: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_dev_users(sm)
    client = await client_for(None)
    await client.get("/login")
    r = await client.post("/auth/dev-login", data={"login_csrf": "wrong", "user": "owner"})
    assert r.status_code == 403


@pytest.mark.parametrize("dev_enabled", [False])
async def test_dev_login_disabled_by_setting(
    engine: object, sm: async_sessionmaker[AsyncSession], dev_enabled: bool
) -> None:
    await _seed_dev_users(sm)
    app = create_app(make_settings(dev_auth_enabled=dev_enabled), engine=engine)  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        page = await client.get("/login")
        assert "دخول التطوير المحلي" not in page.text
        r = await client.post("/auth/dev-login", data={"login_csrf": "x", "user": "owner"})
        assert r.status_code == 400


async def test_security_headers_present(client_for: ClientFactory) -> None:
    r = await (await client_for(None)).get("/login")
    assert r.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
