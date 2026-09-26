"""مساعد تيليجرام: فئة استهلاك «assistant» لأسئلة الأعضاء

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "('discovery', 'new_opportunity', 'followup', 'test')"
_NEW = "('discovery', 'new_opportunity', 'followup', 'test', 'assistant')"


def upgrade() -> None:
    op.execute("ALTER TABLE sales.usage_ledger DROP CONSTRAINT ck_usage_ledger_category")
    op.execute(
        "ALTER TABLE sales.usage_ledger ADD CONSTRAINT ck_usage_ledger_category "
        f"CHECK (category IN {_NEW})"
    )


def downgrade() -> None:
    # لا نحذف سجل الاستهلاك: أسئلة المساعد تُنسب إلى «test» حتى يبقى المجموع صحيحًا.
    op.execute("UPDATE sales.usage_ledger SET category = 'test' WHERE category = 'assistant'")
    op.execute("ALTER TABLE sales.usage_ledger DROP CONSTRAINT ck_usage_ledger_category")
    op.execute(
        "ALTER TABLE sales.usage_ledger ADD CONSTRAINT ck_usage_ledger_category "
        f"CHECK (category IN {_OLD})"
    )
