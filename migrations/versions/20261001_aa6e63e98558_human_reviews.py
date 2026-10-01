"""human reviews

Revision ID: aa6e63e98558
Revises: ec8bd7bbc857
Create Date: 2026-10-01 15:28:09.802887
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "aa6e63e98558"
down_revision: str | None = "ec8bd7bbc857"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "human_reviews",
        sa.Column("experiment_id", sa.UUID(), nullable=False),
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column(
            "decision",
            sa.Enum("approve", "reject", name="reviewdecision", native_enum=False, length=40),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("reviewer", sa.String(length=120), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_human_reviews_experiment_id"), "human_reviews", ["experiment_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_human_reviews_experiment_id"), table_name="human_reviews")
    op.drop_table("human_reviews")
