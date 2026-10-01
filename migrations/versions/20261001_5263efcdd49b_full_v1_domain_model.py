"""full v1 domain model

Revision ID: 5263efcdd49b
Revises: aa6e63e98558
Create Date: 2026-10-01 15:39:31.818758
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "5263efcdd49b"
down_revision: str | None = "aa6e63e98558"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_models",
        sa.Column("provider", sa.String(length=60), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("operation", sa.String(length=60), nullable=False),
        sa.Column("capabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("pricing", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "model", "operation"),
    )
    op.create_table(
        "characters",
        sa.Column("ip_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=60), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["ip_id"],
            ["ips.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ip_id", "name"),
    )
    op.create_index(op.f("ix_characters_ip_id"), "characters", ["ip_id"], unique=False)
    op.create_table(
        "knowledge_summaries",
        sa.Column("ip_id", sa.UUID(), nullable=True),
        sa.Column("topic", sa.String(length=120), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Double(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["ip_id"],
            ["ips.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_knowledge_summaries_ip_id"), "knowledge_summaries", ["ip_id"], unique=False
    )
    op.create_table(
        "platform_accounts",
        sa.Column("ip_id", sa.UUID(), nullable=False),
        sa.Column(
            "platform",
            sa.Enum(
                "youtube_shorts",
                "tiktok",
                "instagram_reels",
                "facebook_reels",
                name="platform",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("external_account_id", sa.String(length=200), nullable=False),
        sa.Column("publisher_profile_id", sa.String(length=200), nullable=True),
        sa.Column("handle", sa.String(length=200), nullable=True),
        sa.Column(
            "status",
            sa.Enum("active", "disconnected", name="accountstatus", native_enum=False, length=40),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["ip_id"],
            ["ips.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ip_id", "platform"),
        sa.UniqueConstraint("platform", "external_account_id"),
    )
    op.create_index(
        op.f("ix_platform_accounts_ip_id"), "platform_accounts", ["ip_id"], unique=False
    )
    op.create_table(
        "character_versions",
        sa.Column("character_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("visual_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["character_id"],
            ["characters.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("character_id", "version"),
    )
    op.create_index(
        op.f("ix_character_versions_character_id"),
        "character_versions",
        ["character_id"],
        unique=False,
    )
    op.create_table(
        "experiment_characters",
        sa.Column("experiment_id", sa.UUID(), nullable=False),
        sa.Column("character_version_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(
            ["character_version_id"],
            ["character_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
        ),
        sa.PrimaryKeyConstraint("experiment_id", "character_version_id"),
    )
    op.create_table(
        "experiment_parents",
        sa.Column("experiment_id", sa.UUID(), nullable=False),
        sa.Column("parent_id", sa.UUID(), nullable=False),
        sa.Column(
            "relation",
            sa.Enum(
                "exploit",
                "mutation",
                "recombination",
                "replication",
                "resurrection",
                name="parentrelation",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("experiment_id <> parent_id", name="no_self_parent"),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["experiments.id"],
        ),
        sa.PrimaryKeyConstraint("experiment_id", "parent_id"),
    )
    op.create_table(
        "job_runs",
        sa.Column("job_type", sa.String(length=60), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                name="jobstatus",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("experiment_id", sa.UUID(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String(length=120), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(op.f("ix_job_runs_experiment_id"), "job_runs", ["experiment_id"], unique=False)
    op.create_index(op.f("ix_job_runs_job_type"), "job_runs", ["job_type"], unique=False)
    op.create_index(op.f("ix_job_runs_status"), "job_runs", ["status"], unique=False)
    op.create_table(
        "knowledge_summary_experiments",
        sa.Column("summary_id", sa.UUID(), nullable=False),
        sa.Column("experiment_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["summary_id"],
            ["knowledge_summaries.id"],
        ),
        sa.PrimaryKeyConstraint("summary_id", "experiment_id"),
    )
    op.create_table(
        "mutations",
        sa.Column("experiment_id", sa.UUID(), nullable=False),
        sa.Column(
            "operation",
            sa.Enum("mutate", "recombine", name="mutationoperation", native_enum=False, length=40),
            nullable=False,
        ),
        sa.Column("gene", sa.String(length=80), nullable=False),
        sa.Column("old_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_experiment_id", sa.UUID(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_experiment_id"],
            ["experiments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_mutations_experiment_id"), "mutations", ["experiment_id"], unique=False
    )
    op.create_table(
        "selection_decisions",
        sa.Column(
            "decision_type",
            sa.Enum(
                "create_experiment",
                "request_replication",
                "conclude_experiment",
                "promote_ip",
                "archive_ip",
                "resurrect_ip",
                name="decisiontype",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "bucket",
            sa.Enum(
                "exploit",
                "mutate",
                "explore",
                name="allocationbucket",
                native_enum=False,
                length=40,
            ),
            nullable=True,
        ),
        sa.Column("ip_id", sa.UUID(), nullable=True),
        sa.Column("subject_experiment_id", sa.UUID(), nullable=True),
        sa.Column("resulting_experiment_id", sa.UUID(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("policy_version", sa.String(length=60), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["ip_id"],
            ["ips.id"],
        ),
        sa.ForeignKeyConstraint(
            ["resulting_experiment_id"],
            ["experiments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["subject_experiment_id"],
            ["experiments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_selection_decisions_ip_id"), "selection_decisions", ["ip_id"], unique=False
    )
    op.create_table(
        "metric_snapshots",
        sa.Column("publication_id", sa.UUID(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hours_since_publication", sa.Double(), nullable=False),
        sa.Column("checkpoint", sa.String(length=20), nullable=True),
        sa.Column("adapter", sa.String(length=60), nullable=False),
        sa.Column("adapter_version", sa.String(length=60), nullable=False),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("normalized", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["publications.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_metric_snapshots_publication_id"),
        "metric_snapshots",
        ["publication_id"],
        unique=False,
    )
    op.create_table(
        "fitness_snapshots",
        sa.Column(
            "scope",
            sa.Enum("video", "ip", name="fitnessscope", native_enum=False, length=40),
            nullable=False,
        ),
        sa.Column("ip_id", sa.UUID(), nullable=False),
        sa.Column("experiment_id", sa.UUID(), nullable=True),
        sa.Column("publication_id", sa.UUID(), nullable=True),
        sa.Column("metric_snapshot_id", sa.UUID(), nullable=True),
        sa.Column(
            "platform",
            sa.Enum(
                "youtube_shorts",
                "tiktok",
                "instagram_reels",
                "facebook_reels",
                name="platform",
                native_enum=False,
                length=40,
            ),
            nullable=True,
        ),
        sa.Column("checkpoint", sa.String(length=20), nullable=True),
        sa.Column("evaluator", sa.String(length=60), nullable=False),
        sa.Column("evaluator_version", sa.String(length=60), nullable=False),
        sa.Column("score", sa.Double(), nullable=False),
        sa.Column("components", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope <> 'video' OR experiment_id IS NOT NULL", name="video_fitness_has_experiment"
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["ip_id"],
            ["ips.id"],
        ),
        sa.ForeignKeyConstraint(
            ["metric_snapshot_id"],
            ["metric_snapshots.id"],
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["publications.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_fitness_snapshots_experiment_id"),
        "fitness_snapshots",
        ["experiment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_fitness_snapshots_ip_id"), "fitness_snapshots", ["ip_id"], unique=False
    )
    # The placeholder string column becomes a real reference to platform_accounts.
    op.alter_column(
        "publications",
        "platform_account_id",
        existing_type=sa.VARCHAR(length=200),
        type_=sa.UUID(),
        existing_nullable=True,
        postgresql_using="platform_account_id::uuid",
    )
    op.create_foreign_key(
        "publications_platform_account_id_fkey",
        "publications",
        "platform_accounts",
        ["platform_account_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("publications_platform_account_id_fkey", "publications", type_="foreignkey")
    op.alter_column(
        "publications",
        "platform_account_id",
        existing_type=sa.UUID(),
        type_=sa.VARCHAR(length=200),
        existing_nullable=True,
    )
    op.drop_index(op.f("ix_fitness_snapshots_ip_id"), table_name="fitness_snapshots")
    op.drop_index(op.f("ix_fitness_snapshots_experiment_id"), table_name="fitness_snapshots")
    op.drop_table("fitness_snapshots")
    op.drop_index(op.f("ix_metric_snapshots_publication_id"), table_name="metric_snapshots")
    op.drop_table("metric_snapshots")
    op.drop_index(op.f("ix_selection_decisions_ip_id"), table_name="selection_decisions")
    op.drop_table("selection_decisions")
    op.drop_index(op.f("ix_mutations_experiment_id"), table_name="mutations")
    op.drop_table("mutations")
    op.drop_table("knowledge_summary_experiments")
    op.drop_index(op.f("ix_job_runs_status"), table_name="job_runs")
    op.drop_index(op.f("ix_job_runs_job_type"), table_name="job_runs")
    op.drop_index(op.f("ix_job_runs_experiment_id"), table_name="job_runs")
    op.drop_table("job_runs")
    op.drop_table("experiment_parents")
    op.drop_table("experiment_characters")
    op.drop_index(op.f("ix_character_versions_character_id"), table_name="character_versions")
    op.drop_table("character_versions")
    op.drop_index(op.f("ix_platform_accounts_ip_id"), table_name="platform_accounts")
    op.drop_table("platform_accounts")
    op.drop_index(op.f("ix_knowledge_summaries_ip_id"), table_name="knowledge_summaries")
    op.drop_table("knowledge_summaries")
    op.drop_index(op.f("ix_characters_ip_id"), table_name="characters")
    op.drop_table("characters")
    op.drop_table("provider_models")
