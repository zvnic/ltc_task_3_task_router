"""add request lifecycle status

Revision ID: 0003_request_status
Revises: 0002_equipment_and_sla
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_request_status"
down_revision: str | None = "0002_equipment_and_sla"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "service_requests",
        sa.Column("status", sa.String(length=24), nullable=False, server_default="new"),
    )


def downgrade() -> None:
    op.drop_column("service_requests", "status")
