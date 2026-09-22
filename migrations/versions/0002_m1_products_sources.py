"""M1: المنتجات والفئات والمصادر والشركات والاستيراد

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# جداول هذه المرحلة؛ تُفعّل عليها RLS دون سياسات (رفض افتراضي لأي دور غير المالك).
TABLES = (
    "products",
    "segments",
    "sources",
    "suppressions",
    "companies",
    "import_batches",
    "product_segments",
    "source_checks",
    "source_segments",
    "company_duplicate_candidates",
    "company_identifiers",
    "company_source_links",
    "contacts",
    "import_rows",
)


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column("problem", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "unavailable_capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "fit_signals",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "exclusions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("product_url", sa.String(length=500), nullable=True),
        sa.Column("demo_url", sa.String(length=500), nullable=True),
        sa.Column(
            "price_status", sa.String(length=16), server_default="needs_review", nullable=False
        ),
        sa.Column("price_text", sa.Text(), server_default="", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="3", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="draft", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("is_demo_data", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("updated_by", sa.String(length=100), nullable=True),
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
        sa.CheckConstraint(
            "price_status IN ('needs_review', 'approved')", name=op.f("ck_products_price_status")
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'archived')", name=op.f("ck_products_status")
        ),
        sa.CheckConstraint("priority BETWEEN 1 AND 5", name=op.f("ck_products_priority")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["sales.workspaces.id"],
            name=op.f("fk_products_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_products_workspace_id_id")),
        sa.UniqueConstraint("workspace_id", "name", name=op.f("uq_products_workspace_id_name")),
        schema="sales",
    )
    op.create_table(
        "segments",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "fit_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "exclusion_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
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
        sa.CheckConstraint("status IN ('active', 'archived')", name=op.f("ck_segments_status")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["sales.workspaces.id"],
            name=op.f("fk_segments_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_segments")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_segments_workspace_id_id")),
        sa.UniqueConstraint("workspace_id", "name", name=op.f("uq_segments_workspace_id_name")),
        schema="sales",
    )
    op.create_table(
        "sources",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("connector_key", sa.String(length=50), nullable=False),
        sa.Column("access_mode", sa.String(length=20), nullable=False),
        sa.Column(
            "allowed_hosts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "allowed_paths",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("max_pages", sa.Integer(), server_default="3", nullable=False),
        sa.Column("max_records", sa.Integer(), server_default="5", nullable=False),
        sa.Column("max_requests", sa.Integer(), server_default="6", nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), server_default="60", nullable=False),
        sa.Column("refresh_interval_hours", sa.Integer(), server_default="24", nullable=False),
        sa.Column("credential_ref", sa.String(length=100), nullable=True),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("store_raw", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("retention_days", sa.Integer(), server_default="90", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="new", nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("config_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("policy_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("policy_notes", sa.Text(), server_default="", nullable=False),
        sa.Column("policy_confirmed_by", sa.String(length=100), nullable=True),
        sa.Column("policy_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("is_demo_data", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("updated_by", sa.String(length=100), nullable=True),
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
        sa.CheckConstraint(
            "access_mode IN ('manual_only', 'public_web', 'api_key', 'oauth')",
            name=op.f("ck_sources_access_mode"),
        ),
        sa.CheckConstraint(
            "kind IN ('manual', 'web_search', 'html', 'rss', 'portal', 'linkedin', 'google_maps')",
            name=op.f("ck_sources_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('new', 'validating', 'sample_ready', 'active', 'needs_setup', 'restricted', 'failed', 'paused')",
            name=op.f("ck_sources_status"),
        ),
        sa.CheckConstraint("max_pages BETWEEN 1 AND 20", name=op.f("ck_sources_max_pages")),
        sa.CheckConstraint("max_records BETWEEN 1 AND 50", name=op.f("ck_sources_max_records")),
        sa.CheckConstraint("max_requests BETWEEN 1 AND 50", name=op.f("ck_sources_max_requests")),
        sa.CheckConstraint("retention_days BETWEEN 1 AND 3650", name=op.f("ck_sources_retention")),
        sa.CheckConstraint("timeout_seconds BETWEEN 5 AND 120", name=op.f("ck_sources_timeout")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["sales.workspaces.id"],
            name=op.f("fk_sources_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_sources_workspace_id_id")),
        sa.UniqueConstraint("workspace_id", "name", name=op.f("uq_sources_workspace_id_name")),
        schema="sales",
    )
    op.create_table(
        "suppressions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("target_hash", sa.LargeBinary(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scope IN ('company', 'domain', 'email', 'phone')", name=op.f("ck_suppressions_scope")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["sales.workspaces.id"],
            name=op.f("fk_suppressions_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_suppressions")),
        sa.UniqueConstraint(
            "workspace_id",
            "scope",
            "target_hash",
            name=op.f("uq_suppressions_workspace_id_scope_target_hash"),
        ),
        schema="sales",
    )
    op.create_table(
        "companies",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("display_name", sa.String(length=300), nullable=False),
        sa.Column("normalized_name", sa.String(length=300), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=True),
        sa.Column("sector", sa.String(length=200), nullable=True),
        sa.Column("region", sa.String(length=200), nullable=True),
        sa.Column("city", sa.String(length=200), nullable=True),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("segment_id", sa.UUID(), nullable=True),
        sa.Column("parent_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column("is_demo_data", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('active', 'needs_review', 'archived')", name=op.f("ck_companies_status")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "parent_id"],
            ["sales.companies.workspace_id", "sales.companies.id"],
            name="fkw_companies_parent_id",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "segment_id"],
            ["sales.segments.workspace_id", "sales.segments.id"],
            name="fkw_companies_segment_id",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["sales.workspaces.id"],
            name=op.f("fk_companies_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companies")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_companies_workspace_id_id")),
        schema="sales",
    )
    op.create_index(
        op.f("ix_companies_workspace_id_normalized_name"),
        "companies",
        ["workspace_id", "normalized_name"],
        unique=False,
        schema="sales",
    )
    op.create_table(
        "import_batches",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("filename", sa.String(length=300), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="preview", nullable=False),
        sa.Column("row_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('csv', 'manual')", name=op.f("ck_import_batches_kind")),
        sa.CheckConstraint(
            "status IN ('preview', 'committed', 'canceled')", name=op.f("ck_import_batches_status")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["sales.sources.workspace_id", "sales.sources.id"],
            name="fkw_import_batches_source_id",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["sales.workspaces.id"],
            name=op.f("fk_import_batches_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_batches")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_import_batches_workspace_id_id")),
        schema="sales",
    )
    op.create_table(
        "product_segments",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column("segment_id", sa.UUID(), nullable=False),
        sa.Column(
            "regions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["sales.products.workspace_id", "sales.products.id"],
            name="fkw_product_segments_product_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "segment_id"],
            ["sales.segments.workspace_id", "sales.segments.id"],
            name="fkw_product_segments_segment_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["sales.workspaces.id"],
            name=op.f("fk_product_segments_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("product_id", "segment_id", name=op.f("pk_product_segments")),
        schema="sales",
    )
    op.create_table(
        "source_checks",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("connector_key", sa.String(length=50), nullable=False),
        sa.Column(
            "sample_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "allowed_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "missing_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "errors",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("request_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("bytes_fetched", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duration_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_amount", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("cost_currency", sa.String(length=3), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "checked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('succeeded', 'needs_setup', 'restricted', 'failed')",
            name=op.f("ck_source_checks_status"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["sales.jobs.id"],
            name=op.f("fk_source_checks_job_id_jobs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["sales.sources.workspace_id", "sales.sources.id"],
            name="fkw_source_checks_source_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["sales.workspaces.id"],
            name=op.f("fk_source_checks_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_checks")),
        schema="sales",
    )
    op.create_index(
        op.f("ix_source_checks_source_id"),
        "source_checks",
        ["source_id", sa.literal_column("checked_at DESC")],
        unique=False,
        schema="sales",
    )
    op.create_table(
        "source_segments",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("segment_id", sa.UUID(), nullable=False),
        sa.Column(
            "regions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "segment_id"],
            ["sales.segments.workspace_id", "sales.segments.id"],
            name="fkw_source_segments_segment_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["sales.sources.workspace_id", "sales.sources.id"],
            name="fkw_source_segments_source_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["sales.workspaces.id"],
            name=op.f("fk_source_segments_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("source_id", "segment_id", name=op.f("pk_source_segments")),
        schema="sales",
    )
    op.create_table(
        "company_duplicate_candidates",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("candidate_company_id", sa.UUID(), nullable=False),
        sa.Column(
            "reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
        sa.Column("decided_by", sa.String(length=100), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('open', 'merged', 'dismissed')",
            name=op.f("ck_company_duplicate_candidates_status"),
        ),
        sa.CheckConstraint(
            "company_id <> candidate_company_id",
            name=op.f("ck_company_duplicate_candidates_distinct"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "candidate_company_id"],
            ["sales.companies.workspace_id", "sales.companies.id"],
            name="fkw_company_duplicate_candidates_candidate_company_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "company_id"],
            ["sales.companies.workspace_id", "sales.companies.id"],
            name="fkw_company_duplicate_candidates_company_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["sales.workspaces.id"],
            name=op.f("fk_company_duplicate_candidates_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_duplicate_candidates")),
        sa.UniqueConstraint(
            "company_id",
            "candidate_company_id",
            name=op.f("uq_company_duplicate_candidates_company_id_candidate_company_id"),
        ),
        schema="sales",
    )
    op.create_table(
        "company_identifiers",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("normalized_value", sa.String(length=500), nullable=False),
        sa.Column("strength", sa.String(length=10), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "strength IN ('strong', 'weak')", name=op.f("ck_company_identifiers_strength")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "company_id"],
            ["sales.companies.workspace_id", "sales.companies.id"],
            name="fkw_company_identifiers_company_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["sales.workspaces.id"],
            name=op.f("fk_company_identifiers_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_identifiers")),
        sa.UniqueConstraint(
            "company_id",
            "kind",
            "normalized_value",
            name=op.f("uq_company_identifiers_company_id_kind_normalized_value"),
        ),
        schema="sales",
    )
    op.create_index(
        op.f("ix_company_identifiers_workspace_id_kind_normalized_value"),
        "company_identifiers",
        ["workspace_id", "kind", "normalized_value"],
        unique=False,
        schema="sales",
    )
    op.create_index(
        "uq_company_identifiers_strong",
        "company_identifiers",
        ["workspace_id", "kind", "normalized_value"],
        unique=True,
        schema="sales",
        postgresql_where=sa.text("strength = 'strong'"),
    )
    op.create_table(
        "company_source_links",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("source_record_ref", sa.String(length=500), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=True),
        sa.Column("import_batch_id", sa.UUID(), nullable=True),
        sa.Column(
            "first_seen_at",
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
        sa.ForeignKeyConstraint(
            ["workspace_id", "company_id"],
            ["sales.companies.workspace_id", "sales.companies.id"],
            name="fkw_company_source_links_company_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["sales.sources.workspace_id", "sales.sources.id"],
            name="fkw_company_source_links_source_id",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["sales.workspaces.id"],
            name=op.f("fk_company_source_links_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_source_links")),
        sa.UniqueConstraint(
            "company_id",
            "source_id",
            "source_record_ref",
            name=op.f("uq_company_source_links_company_id_source_id_source_record_ref"),
        ),
        schema="sales",
    )
    op.create_table(
        "contacts",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=True),
        sa.Column("professional_name", sa.String(length=200), nullable=True),
        sa.Column("role_title", sa.String(length=200), nullable=True),
        sa.Column("channel", sa.String(length=10), nullable=False),
        sa.Column("value", sa.String(length=320), nullable=False),
        sa.Column("value_hash", sa.LargeBinary(), nullable=False),
        sa.Column("normalized", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("channel IN ('email', 'phone')", name=op.f("ck_contacts_channel")),
        sa.ForeignKeyConstraint(
            ["workspace_id", "company_id"],
            ["sales.companies.workspace_id", "sales.companies.id"],
            name="fkw_contacts_company_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["sales.workspaces.id"],
            name=op.f("fk_contacts_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contacts")),
        sa.UniqueConstraint(
            "workspace_id",
            "company_id",
            "channel",
            "value_hash",
            name=op.f("uq_contacts_workspace_id_company_id_channel_value_hash"),
        ),
        schema="sales",
    )
    op.create_index(
        op.f("ix_contacts_workspace_id_channel_value_hash"),
        "contacts",
        ["workspace_id", "channel", "value_hash"],
        unique=False,
        schema="sales",
    )
    op.create_table(
        "import_rows",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("display_name", sa.String(length=300), server_default="", nullable=False),
        sa.Column(
            "errors",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "match",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("result_company_id", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "action IN ('create', 'link', 'review', 'skip_error', 'skip_suppressed', 'skip_duplicate_in_file', 'skip_conflict')",
            name=op.f("ck_import_rows_action"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "batch_id"],
            ["sales.import_batches.workspace_id", "sales.import_batches.id"],
            name="fkw_import_rows_batch_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["sales.workspaces.id"],
            name=op.f("fk_import_rows_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_rows")),
        sa.UniqueConstraint(
            "batch_id", "row_number", name=op.f("uq_import_rows_batch_id_row_number")
        ),
        schema="sales",
    )

    for table in TABLES:
        op.execute(f"ALTER TABLE sales.{table} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("import_rows", schema="sales")
    op.drop_table("contacts", schema="sales")
    op.drop_table("company_source_links", schema="sales")
    op.drop_table("company_identifiers", schema="sales")
    op.drop_table("company_duplicate_candidates", schema="sales")
    op.drop_table("source_segments", schema="sales")
    op.drop_table("source_checks", schema="sales")
    op.drop_table("product_segments", schema="sales")
    op.drop_table("import_batches", schema="sales")
    op.drop_table("companies", schema="sales")
    op.drop_table("suppressions", schema="sales")
    op.drop_table("sources", schema="sales")
    op.drop_table("segments", schema="sales")
    op.drop_table("products", schema="sales")
