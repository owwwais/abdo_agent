from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool, text

from app.db import models, models_ops, models_sales  # noqa: F401  تسجيل الجداول في metadata
from app.db.base import SCHEMA, Base
from app.db.session import normalize_db_url

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = config.attributes.get("database_url") or os.environ.get("DATABASE_URL")
    if not url:
        from app.config import get_settings

        url = get_settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL غير مضبوط")
    if not config.attributes.get("database_url"):
        # يطبع الخادم الهدف (دون كلمة المرور) كي لا يُرحَّل الخادم الخطأ دون انتباه؛ .env قد يشير للإنتاج.
        from urllib.parse import urlsplit

        host = urlsplit(url.replace("+psycopg", "").replace("+asyncpg", "")).hostname
        print(f"alembic target database host: {host}", file=sys.stderr)
    return normalize_db_url(url)


def _include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    # جداول LangGraph وأي مخطط آخر خارج نطاق ترحيلات CRM.
    if type_ == "table" and getattr(obj, "schema", None) not in (None, SCHEMA):
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        version_table_schema=SCHEMA,
        include_schemas=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=SCHEMA,
            include_schemas=True,
            include_object=_include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
