"""M0: الهوية والعضويات والجلسات والطابور والتدقيق

Revision ID: 0001
Revises:
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# جداول هذه المرحلة؛ تُفعّل عليها RLS دون سياسات (رفض افتراضي لأي دور غير المالك).
TABLES = (
    "user_profiles",
    "worker_heartbeats",
    "workspaces",
    "audit_log",
    "jobs",
    "memberships",
    "web_sessions",
)


def upgrade() -> None:
    op.create_table(
        "user_profiles",
        sa.Column("auth_user_id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("is_dev_fixture", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("auth_user_id", name=op.f("pk_user_profiles")),
        sa.UniqueConstraint("email", name=op.f("uq_user_profiles_email")),
        sa.UniqueConstraint("telegram_user_id", name=op.f("uq_user_profiles_telegram_user_id")),
        schema="sales",
    )
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=100), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("current_job_id", sa.UUID(), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("worker_id", name=op.f("pk_worker_heartbeats")),
        schema="sales",
    )
    op.create_table(
        "workspaces",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("timezone", sa.String(length=64), server_default="Asia/Riyadh", nullable=False),
        sa.Column("operating_mode", sa.String(length=16), server_default="demo", nullable=False),
        sa.Column("default_phone_region", sa.String(length=2), nullable=True),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("settings_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("is_demo_data", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("operating_mode IN ('demo', 'live')", name=op.f("ck_workspaces_mode")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
        schema="sales",
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=100), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=True),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("entity_version", sa.Integer(), nullable=True),
        sa.Column(
            "redacted_change",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "actor_type IN ('user', 'service', 'system')", name=op.f("ck_audit_log_actor_type")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["sales.workspaces.id"],
            name=op.f("fk_audit_log_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
        schema="sales",
    )
    op.create_index(
        op.f("ix_audit_log_entity_id"), "audit_log", ["entity_id"], unique=False, schema="sales"
    )
    op.create_index(
        op.f("ix_audit_log_workspace_id"),
        "audit_log",
        ["workspace_id", sa.literal_column("occurred_at DESC")],
        unique=False,
        schema="sales",
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'retry_scheduled', 'completed', 'failed', 'canceled')",
            name=op.f("ck_jobs_status"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["sales.workspaces.id"],
            name=op.f("fk_jobs_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_jobs_idempotency_key")),
        schema="sales",
    )
    op.create_index(
        "ix_jobs_claimable",
        "jobs",
        ["run_after"],
        unique=False,
        schema="sales",
        postgresql_where=sa.text("status IN ('queued', 'retry_scheduled')"),
    )
    op.create_index(
        op.f("ix_jobs_workspace_id_kind_status"),
        "jobs",
        ["workspace_id", "kind", "status"],
        unique=False,
        schema="sales",
    )
    op.create_table(
        "memberships",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("auth_user_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('owner', 'reviewer')", name=op.f("ck_memberships_role")),
        sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f("ck_memberships_status")),
        sa.ForeignKeyConstraint(
            ["auth_user_id"],
            ["sales.user_profiles.auth_user_id"],
            name=op.f("fk_memberships_auth_user_id_user_profiles"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["sales.workspaces.id"],
            name=op.f("fk_memberships_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memberships")),
        sa.UniqueConstraint(
            "workspace_id", "auth_user_id", name=op.f("uq_memberships_workspace_id_auth_user_id")
        ),
        schema="sales",
    )
    op.create_table(
        "web_sessions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("auth_user_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("auth_method", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "auth_method IN ('supabase', 'dev')", name=op.f("ck_web_sessions_auth_method")
        ),
        sa.ForeignKeyConstraint(
            ["auth_user_id"],
            ["sales.user_profiles.auth_user_id"],
            name=op.f("fk_web_sessions_auth_user_id_user_profiles"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["sales.workspaces.id"],
            name=op.f("fk_web_sessions_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_web_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_web_sessions_token_hash")),
        schema="sales",
    )

    for table in TABLES:
        op.execute(f"ALTER TABLE sales.{table} ENABLE ROW LEVEL SECURITY")
    # أدوار Supabase العامة لا تصل لمخطط التشغيل حتى لو أُضيف للـData API بالخطأ.
    op.execute(
        """
        DO $$
        DECLARE r text;
        BEGIN
          FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
              EXECUTE format('REVOKE ALL ON SCHEMA sales FROM %I', r);
              EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA sales FROM %I', r);
              EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA sales REVOKE ALL ON TABLES FROM %I', r);
            END IF;
          END LOOP;
        END $$;
        """
    )
    op.execute("REVOKE ALL ON SCHEMA sales FROM PUBLIC")


def downgrade() -> None:
    op.drop_table("web_sessions", schema="sales")
    op.drop_table("memberships", schema="sales")
    op.drop_table("jobs", schema="sales")
    op.drop_table("audit_log", schema="sales")
    op.drop_table("workspaces", schema="sales")
    op.drop_table("worker_heartbeats", schema="sales")
    op.drop_table("user_profiles", schema="sales")
