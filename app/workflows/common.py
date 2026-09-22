"""اعتماديات مسارات LangGraph: الإعدادات، جلسات القاعدة، البوابة، ونقل الشبكة (للاختبار)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.gateway import ModelGateway
from app.config import Settings
from app.connectors.netguard import Resolver
from app.db.models import Workspace


@dataclass
class Deps:
    settings: Settings
    sm: async_sessionmaker[AsyncSession]
    resolver: Resolver | None = None
    transport: httpx.AsyncBaseTransport | None = None
    gateway_factory: type[ModelGateway] | None = None

    def gateway(self, workspace_id: uuid.UUID) -> ModelGateway:
        factory = self.gateway_factory or ModelGateway
        return factory(self.settings, self.sm, workspace_id)


async def workspace_tz(
    sm: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID, default: str
) -> str:
    async with sm() as db:
        ws = await db.get(Workspace, workspace_id)
        return ws.timezone if ws else default


def local_today(tz_name: str, now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(ZoneInfo(tz_name)).date()
