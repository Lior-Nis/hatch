"""experiment output requirements

Revision ID: 5c6bfa94641f
Revises: 6883d2d0add8
Create Date: 2026-10-01 15:16:02.245475
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "5c6bfa94641f"
down_revision: str | None = "6883d2d0add8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "experiments",
        sa.Column(
            "output_requirements",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("experiments", "output_requirements", server_default=None)


def downgrade() -> None:
    op.drop_column("experiments", "output_requirements")
