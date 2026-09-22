from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings


def normalize_db_url(url: str) -> str:
    """يقبل postgresql:// أو postgres:// ويحوله إلى مشغل psycopg 3."""
    for prefix in ("postgresql+psycopg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def create_engine(settings: Settings, **kwargs: Any) -> AsyncEngine:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL غير مضبوط")
    return create_async_engine(
        normalize_db_url(settings.database_url),
        pool_size=settings.db_pool_size,
        pool_pre_ping=True,
        **kwargs,
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def transaction(sm: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with sm() as session, session.begin():
        yield session


def loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    """psycopg غير المتزامن لا يعمل مع ProactorEventLoop الافتراضي في Windows."""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop
    return None
