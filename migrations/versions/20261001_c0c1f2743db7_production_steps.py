"""production steps

Revision ID: c0c1f2743db7
Revises: 0ce02c5f9805
Create Date: 2026-10-01 16:18:40.142335
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c0c1f2743db7"
down_revision: str | None = "0ce02c5f9805"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "production_steps",
        sa.Column("experiment_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "succeeded",
                "failed",
                name="stepstatus",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", "key"),
    )
    op.create_index(
        op.f("ix_production_steps_experiment_id"),
        "production_steps",
        ["experiment_id"],
        unique=False,
    )
    op.add_column("generation_attempts", sa.Column("step_id", sa.UUID(), nullable=True))
    op.add_column(
        "generation_attempts",
        sa.Column("strategy", sa.String(length=40), nullable=False, server_default="original"),
    )
    op.alter_column("generation_attempts", "strategy", server_default=None)
    op.create_foreign_key(
        "generation_attempts_step_id_fkey",
        "generation_attempts",
        "production_steps",
        ["step_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "generation_attempts_step_id_fkey", "generation_attempts", type_="foreignkey"
    )
    op.drop_column("generation_attempts", "strategy")
    op.drop_column("generation_attempts", "step_id")
    op.drop_index(op.f("ix_production_steps_experiment_id"), table_name="production_steps")
    op.drop_table("production_steps")
