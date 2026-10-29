"""Client media plans read from Google Sheets."""

import sqlalchemy as sa
from alembic import op

revision = "c64f0a8d2e17"
down_revision = "b91e5d3c7a26"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "media_plans",
        sa.Column("app_mode", sa.String(20), primary_key=True),
        sa.Column("client_id", sa.String(32), primary_key=True),
        sa.Column("url", sa.String(500), nullable=True),
        sa.Column("plan", sa.JSON(), nullable=True),
        sa.Column("source_hash", sa.String(64), nullable=True),
        sa.Column("error", sa.String(300), nullable=True),
        sa.Column("dismissed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.String(30), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("media_plans")
