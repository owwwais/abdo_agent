"""M0: قاعدة جديدة تطبق ترحيلاتها، لا انحراف بين النماذج والترحيلات، وRLS مفعلة على كل الجداول."""

from __future__ import annotations

from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.base import Base
from tests.conftest import alembic_config


async def test_all_model_tables_exist_with_rls(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'sales' AND c.relkind = 'r' AND c.relname <> 'alembic_version'"
                )
            )
        ).all()
    tables = {r.relname: r.relrowsecurity for r in rows}
    assert set(tables) == {t.name for t in Base.metadata.sorted_tables}
    assert all(tables.values()), [t for t, rls in tables.items() if not rls]


def test_no_drift_between_models_and_migrations(migrated_db: str) -> None:
    command.check(alembic_config())  # يرفع استثناء إن وُجدت عمليات ترحيل غير مكتوبة


async def test_public_role_has_no_schema_access(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        allowed = (
            await conn.execute(text("SELECT has_schema_privilege('public', 'sales', 'USAGE')"))
        ).scalar_one()
    assert allowed is False
