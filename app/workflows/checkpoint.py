"""حافظ حالة LangGraph الدائم في PostgreSQL (مخطط sales_graph المستقل عن CRM).

اتصال autocommit مخصص بـsearch_path=sales_graph لأن جداول الحافظ غير مؤهلة بمخطط. يُضبط بأمر SET بعد
الاتصال لا بمعامل بدء (options=-c …) لأن بعض الـpoolers (مثل Supavisor) قد ترفض معاملات البدء. مع Supabase
استخدم اتصالًا مباشرًا أو session pooler (لا transaction pooler): الجلسة المخصصة تحفظ SET طوال الاتصال.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from app.config import Settings

GRAPH_SCHEMA = "sales_graph"


def conninfo(settings: Settings) -> str:
    url = settings.checkpoint_database_url or settings.database_url
    for prefix in ("postgresql+psycopg://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql://" + url[len(prefix) :]
    return url


@asynccontextmanager
async def checkpointer(settings: Settings) -> AsyncIterator[AsyncPostgresSaver]:
    conn = await AsyncConnection.connect(
        conninfo(settings), autocommit=True, prepare_threshold=0, row_factory=dict_row
    )
    try:
        await conn.execute("SET search_path TO sales_graph")  # = GRAPH_SCHEMA
        yield AsyncPostgresSaver(conn=conn)
    finally:
        await conn.close()


async def setup_checkpointer(settings: Settings) -> None:
    """ينشئ جداول الحافظ إن لم توجد (idempotent). يُستدعى عند بدء العامل."""
    async with checkpointer(settings) as saver:
        await saver.setup()
