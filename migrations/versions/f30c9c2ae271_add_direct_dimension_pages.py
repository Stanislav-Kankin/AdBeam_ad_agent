"""Store resumable daily Direct dimension pages."""

import sqlalchemy as sa
from alembic import op

revision = "f30c9c2ae271"
down_revision = "e19a72c4b6d0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "direct_dimension_pages",
        sa.Column("app_mode", sa.String(20), primary_key=True),
        sa.Column("client_id", sa.String(32), primary_key=True),
        sa.Column("day", sa.String(10), primary_key=True),
        sa.Column("dimension", sa.String(20), primary_key=True),
        sa.Column("page", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("last_page", sa.Boolean(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refresh_after", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_direct_dimension_pages_refresh_after",
        "direct_dimension_pages",
        ["refresh_after"],
    )


def downgrade():
    op.drop_index("ix_direct_dimension_pages_refresh_after", table_name="direct_dimension_pages")
    op.drop_table("direct_dimension_pages")
