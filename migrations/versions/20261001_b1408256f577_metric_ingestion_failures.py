"""metric ingestion failures

Revision ID: b1408256f577
Revises: c88ff7c02335
Create Date: 2026-10-01 16:41:02.139955
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1408256f577"
down_revision: str | None = "c88ff7c02335"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "metric_ingestion_failures",
        sa.Column("publication_id", sa.UUID(), nullable=False),
        sa.Column("checkpoint", sa.String(length=20), nullable=True),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["publications.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_metric_ingestion_failures_publication_id"),
        "metric_ingestion_failures",
        ["publication_id"],
        unique=False,
    )
    op.create_index(
        "ix_metric_snapshots_normalized",
        "metric_snapshots",
        ["normalized"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_metric_snapshots_raw", "metric_snapshots", ["raw"], unique=False, postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_metric_snapshots_raw", table_name="metric_snapshots", postgresql_using="gin")
    op.drop_index(
        "ix_metric_snapshots_normalized", table_name="metric_snapshots", postgresql_using="gin"
    )
    op.drop_index(
        op.f("ix_metric_ingestion_failures_publication_id"), table_name="metric_ingestion_failures"
    )
    op.drop_table("metric_ingestion_failures")
