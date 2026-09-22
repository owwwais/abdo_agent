"""تهيئة الاختبارات: قاعدة PostgreSQL حقيقية (sales_test) تُبنى من الترحيلات في كل تشغيل، وتُفرغ قبل كل اختبار.

محليًا: uv run python scripts/localdb.py start   (أو TEST_DATABASE_URL لقاعدة أخرى، مثل خدمة CI)
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.auth.sessions import SESSION_COOKIE, create_session
from app.config import Settings
from app.db.models import Membership, UserProfile, Workspace
from app.db.session import normalize_db_url
from app.main import create_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DB_URL = normalize_db_url(
    os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://postgres@127.0.0.1:54329/sales_test")
)

if sys.platform == "win32":
    # psycopg غير المتزامن يحتاج SelectorEventLoop في Windows.
    def pytest_asyncio_loop_factories(
        config: Any, item: Any
    ) -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
        return {"selector": asyncio.SelectorEventLoop}


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "database_url": TEST_DB_URL,
        "dev_auth_enabled": True,
        "source_fetch_enabled": True,
        "app_base_url": "http://testserver",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def alembic_config() -> Config:
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    cfg.attributes["database_url"] = TEST_DB_URL
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def migrated_db() -> str:
    """قاعدة جديدة: حذف المخطط ثم تطبيق كل الترحيلات من الصفر (قبول M0: قاعدة جديدة تطبق ترحيلاتها)."""
    engine = create_sync_engine(TEST_DB_URL)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS sales CASCADE"))
    except Exception as exc:
        pytest.exit(
            f"تعذر الاتصال بقاعدة الاختبار {TEST_DB_URL}: {exc}\n"
            "شغّل: uv run python scripts/localdb.py start  أو اضبط TEST_DATABASE_URL",
            returncode=3,
        )
    finally:
        engine.dispose()
    command.upgrade(alembic_config(), "head")
    return TEST_DB_URL


@pytest.fixture(scope="session")
def settings() -> Settings:
    return make_settings()


@pytest_asyncio.fixture(scope="session")
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db, pool_size=10)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(scope="session")
async def sm(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def _clean(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    if "engine" not in request.fixturenames and "db_needed" not in request.keywords:
        yield
        return
    eng: AsyncEngine = request.getfixturevalue("engine")
    async with eng.begin() as conn:
        tables = (
            (
                await conn.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'sales' AND tablename <> 'alembic_version'"
                    )
                )
            )
            .scalars()
            .all()
        )
        if tables:
            await conn.execute(
                text("TRUNCATE " + ", ".join(f"sales.{t}" for t in tables) + " CASCADE")
            )
    yield


@dataclass
class Member:
    auth_user_id: uuid.UUID
    role: str
    email: str


@dataclass
class WorkspaceFixture:
    id: uuid.UUID
    owner: Member
    reviewer: Member


WorkspaceFactory = Callable[..., Awaitable[WorkspaceFixture]]


@pytest_asyncio.fixture
async def make_workspace(sm: async_sessionmaker[AsyncSession]) -> WorkspaceFactory:
    async def factory(
        name: str = "شركة اختبار", phone_region: str | None = "SA"
    ) -> WorkspaceFixture:
        async with sm() as db, db.begin():
            ws = Workspace(name=name, default_phone_region=phone_region)
            db.add(ws)
            await db.flush()
            members = {}
            for role in ("owner", "reviewer"):
                uid = uuid.uuid4()
                email = f"{role}-{uid.hex[:8]}@test.local"
                db.add(UserProfile(auth_user_id=uid, email=email, display_name=f"{role} {name}"))
                await db.flush()
                db.add(Membership(workspace_id=ws.id, auth_user_id=uid, role=role, status="active"))
                members[role] = Member(uid, role, email)
            return WorkspaceFixture(ws.id, members["owner"], members["reviewer"])

    return factory


@pytest_asyncio.fixture
async def ws(make_workspace: WorkspaceFactory) -> WorkspaceFixture:
    return await make_workspace()


@pytest.fixture
def app(settings: Settings, engine: AsyncEngine) -> Any:
    return create_app(settings, engine=engine)


ClientFactory = Callable[..., Awaitable[httpx.AsyncClient]]


@pytest_asyncio.fixture
async def client_for(
    app: Any, sm: async_sessionmaker[AsyncSession], settings: Settings
) -> AsyncIterator[ClientFactory]:
    """عميل HTTP بجلسة مسجلة لعضو معين؛ يرسل رمز CSRF تلقائيًا في ترويسة JSON."""
    clients: list[httpx.AsyncClient] = []

    async def factory(
        member: Member | None = None, *, csrf: bool = True, client_host: str = "127.0.0.1"
    ) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=app, client=(client_host, 50000))
        client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
        if member is not None:
            async with sm() as db:
                token = await create_session(db, settings, member.auth_user_id, "dev")
                await db.commit()
            client.cookies.set(SESSION_COOKIE, token)
            if csrf:
                me = (await client.get("/api/me")).json()
                client.headers["X-CSRF-Token"] = me["csrf_token"]
        clients.append(client)
        return client

    yield factory
    for c in clients:
        await c.aclose()
