"""budget ledger status

Revision ID: ec8bd7bbc857
Revises: 5c6bfa94641f
Create Date: 2026-10-01 15:17:53.276395
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ec8bd7bbc857"
down_revision: str | None = "5c6bfa94641f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    status = sa.Enum(
        "reserved", "settled", "blocked", name="ledgerstatus", native_enum=False, length=40
    )
    # Rows that predate the status column were recorded after the fact: settled.
    op.add_column(
        "budget_ledger", sa.Column("status", status, nullable=False, server_default="settled")
    )
    op.alter_column("budget_ledger", "status", server_default=None)
    op.add_column(
        "budget_ledger",
        sa.Column("block_reason", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("budget_ledger", "block_reason")
    op.drop_column("budget_ledger", "status")
