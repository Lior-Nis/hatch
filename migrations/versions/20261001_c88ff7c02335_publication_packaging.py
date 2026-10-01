"""publication packaging

Revision ID: c88ff7c02335
Revises: c0c1f2743db7
Create Date: 2026-10-01 16:32:46.688275
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c88ff7c02335"
down_revision: str | None = "c0c1f2743db7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "publications",
        sa.Column(
            "package",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("publications", "package", server_default=None)
    op.add_column("publications", sa.Column("publisher", sa.String(length=60), nullable=True))
    op.add_column("publications", sa.Column("permalink", sa.Text(), nullable=True))
    op.add_column("publications", sa.Column("error", sa.Text(), nullable=True))
    op.create_unique_constraint(
        "publications_experiment_id_platform_key", "publications", ["experiment_id", "platform"]
    )


def downgrade() -> None:
    # WARNING: constraint name is None; this directive will fail as
    # rendered.  Add a name, or use a naming convention; see
    # https://alembic.sqlalchemy.org/en/latest/naming.html
    op.drop_constraint("publications_experiment_id_platform_key", "publications", type_="unique")
    op.drop_column("publications", "error")
    op.drop_column("publications", "permalink")
    op.drop_column("publications", "publisher")
    op.drop_column("publications", "package")
