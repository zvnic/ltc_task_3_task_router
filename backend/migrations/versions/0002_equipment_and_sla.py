"""Add optional equipment inventory and request completion SLA."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_equipment_and_sla"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "service_requests",
        sa.Column(
            "required_equipment",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "service_requests",
        sa.Column("completion_deadline", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "engineers",
        sa.Column(
            "equipment_inventory",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("engineers", "equipment_inventory")
    op.drop_column("service_requests", "completion_deadline")
    op.drop_column("service_requests", "required_equipment")
