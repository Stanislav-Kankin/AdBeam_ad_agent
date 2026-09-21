"""Persist Telegram user grants and revocations."""

import sqlalchemy as sa
from alembic import op

revision = "fb8a42d19c53"
down_revision = "fa7d31c08b42"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bot_users",
        sa.Column("app_mode", sa.String(20), primary_key=True),
        sa.Column("user_id", sa.String(30), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("client_ids", sa.JSON(), nullable=False),
        sa.Column("updated_by", sa.String(30), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("bot_users")
