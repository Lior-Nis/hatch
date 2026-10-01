"""model calls

Revision ID: 30b15b3bca7b
Revises: 44024646588b
Create Date: 2026-10-01 15:56:58.339315
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "30b15b3bca7b"
down_revision: str | None = "44024646588b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_calls",
        sa.Column("purpose", sa.String(length=80), nullable=False),
        sa.Column("provider", sa.String(length=60), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("system", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("ledger_entry_id", sa.UUID(), nullable=True),
        sa.Column("experiment_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["ledger_entry_id"],
            ["budget_ledger.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_model_calls_experiment_id"), "model_calls", ["experiment_id"], unique=False
    )
    op.create_index(op.f("ix_model_calls_purpose"), "model_calls", ["purpose"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_model_calls_purpose"), table_name="model_calls")
    op.drop_index(op.f("ix_model_calls_experiment_id"), table_name="model_calls")
    op.drop_table("model_calls")
