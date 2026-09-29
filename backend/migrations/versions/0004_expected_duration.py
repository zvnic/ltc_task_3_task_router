"""add expected service duration

Revision ID: 0004_expected_duration
Revises: 0003_request_status
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_expected_duration"
down_revision: str | None = "0003_request_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "service_requests",
        sa.Column("expected_duration_minutes", sa.Integer(), nullable=True),
    )
    op.execute(
        "UPDATE service_requests "
        "SET expected_duration_minutes = duration_minutes "
        "WHERE expected_duration_minutes IS NULL"
    )


def downgrade() -> None:
    op.drop_column("service_requests", "expected_duration_minutes")
