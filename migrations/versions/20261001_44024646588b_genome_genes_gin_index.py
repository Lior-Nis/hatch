"""genome genes gin index

Revision ID: 44024646588b
Revises: 84af3c998069
Create Date: 2026-10-01 15:52:06.639026
"""

from collections.abc import Sequence

from alembic import op

revision: str = "44024646588b"
down_revision: str | None = "84af3c998069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_creative_genomes_genes",
        "creative_genomes",
        ["genes"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_creative_genomes_genes", table_name="creative_genomes", postgresql_using="gin"
    )
