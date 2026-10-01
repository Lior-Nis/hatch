"""video status changed at

Revision ID: 84af3c998069
Revises: 5263efcdd49b
Create Date: 2026-10-01 15:49:36.000043
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "84af3c998069"
down_revision: str | None = "5263efcdd49b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "experiments",
        sa.Column(
            "video_status_changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.alter_column("experiments", "video_status_changed_at", server_default=None)


def downgrade() -> None:
    op.drop_column("experiments", "video_status_changed_at")
