"""experiment production plan

Revision ID: 0ce02c5f9805
Revises: 30b15b3bca7b
Create Date: 2026-10-01 16:14:14.665542
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0ce02c5f9805"
down_revision: str | None = "30b15b3bca7b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "experiments",
        sa.Column(
            "production_plan",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("experiments", "production_plan", server_default=None)


def downgrade() -> None:
    op.drop_column("experiments", "production_plan")
