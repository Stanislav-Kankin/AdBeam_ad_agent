"""Add composable daily analytics snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "e19a72c4b6d0"
down_revision = "d1840f2e917b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "daily_snapshots",
        sa.Column("app_mode", sa.String(20), primary_key=True),
        sa.Column("client_id", sa.String(32), primary_key=True),
        sa.Column("day", sa.String(10), primary_key=True),
        sa.Column("quality", sa.String(10), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refresh_after", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_daily_snapshots_refresh_after", "daily_snapshots", ["refresh_after"])


def downgrade():
    op.drop_index("ix_daily_snapshots_refresh_after", table_name="daily_snapshots")
    op.drop_table("daily_snapshots")
