"""Initial domain schema."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "datasets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("planning_date", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("office", postgresql.JSONB(), nullable=False),
        sa.Column("import_report", postgresql.JSONB(), nullable=False),
        sa.Column("assumptions", postgresql.JSONB(), nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "service_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.Uuid(),
            sa.ForeignKey("datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(120), nullable=False),
        sa.Column("input_order", sa.Integer(), nullable=False),
        sa.Column("address_raw", sa.String(600), nullable=False),
        sa.Column("address_normalized", sa.String(600), nullable=False),
        sa.Column("district", sa.String(160), nullable=False),
        sa.Column("coordinates", postgresql.JSONB(), nullable=False),
        sa.Column("coordinate_source", sa.String(24), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("required_skill", sa.String(32), nullable=False),
        sa.Column("required_transport", sa.String(32), nullable=True),
        sa.Column("priority", sa.String(16), nullable=False),
        sa.Column("source_fields", postgresql.JSONB(), nullable=False),
        sa.Column("enrichment_rule_version", sa.String(32), nullable=False),
        sa.UniqueConstraint("dataset_id", "external_id"),
    )
    op.create_index("ix_service_requests_dataset", "service_requests", ["dataset_id"])
    op.create_table(
        "engineers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.Uuid(),
            sa.ForeignKey("datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(120), nullable=False),
        sa.Column("input_order", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("start_location", postgresql.JSONB(), nullable=False),
        sa.Column("shift_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("shift_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("skills", postgresql.JSONB(), nullable=False),
        sa.Column("transport", sa.String(32), nullable=False),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("dataset_id", "external_id"),
    )
    op.create_index("ix_engineers_dataset", "engineers", ["dataset_id"])
    op.create_table(
        "reference_assignments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.Uuid(),
            sa.ForeignKey("datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "request_id",
            sa.Uuid(),
            sa.ForeignKey("service_requests.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reference_external_id", sa.String(120), nullable=False),
        sa.Column("team_name", sa.String(200), nullable=True),
        sa.Column("reference_status", sa.String(80), nullable=False),
        sa.Column("match_status", sa.String(24), nullable=False),
        sa.Column("source_fields", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_reference_assignments_dataset", "reference_assignments", ["dataset_id"])
    op.create_table(
        "plans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.Uuid(),
            sa.ForeignKey("datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("input_revision", sa.Integer(), nullable=False),
        sa.Column("parent_plan_id", sa.Uuid(), sa.ForeignKey("plans.id"), nullable=True),
        sa.Column("baseline_plan_id", sa.Uuid(), sa.ForeignKey("plans.id"), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column("model_info", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_plans_dataset", "plans", ["dataset_id"])
    op.create_table(
        "planning_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.Uuid(),
            sa.ForeignKey("datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "base_plan_id", sa.Uuid(), sa.ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("result_plan_id", sa.Uuid(), sa.ForeignKey("plans.id"), nullable=True),
        sa.UniqueConstraint("dataset_id", "idempotency_key"),
    )


def downgrade() -> None:
    op.drop_table("planning_events")
    op.drop_index("ix_plans_dataset", table_name="plans")
    op.drop_table("plans")
    op.drop_index("ix_reference_assignments_dataset", table_name="reference_assignments")
    op.drop_table("reference_assignments")
    op.drop_index("ix_engineers_dataset", table_name="engineers")
    op.drop_table("engineers")
    op.drop_index("ix_service_requests_dataset", table_name="service_requests")
    op.drop_table("service_requests")
    op.drop_table("datasets")
