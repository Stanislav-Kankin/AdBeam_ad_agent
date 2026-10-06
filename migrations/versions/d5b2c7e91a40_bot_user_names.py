"""Telegram name and @username of bot users, so the admin list is readable."""

import sqlalchemy as sa
from alembic import op

revision = "d5b2c7e91a40"
down_revision = "c3e8a1d4f602"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("bot_users") as batch:
        batch.add_column(sa.Column("username", sa.String(64), nullable=True))
        batch.add_column(sa.Column("full_name", sa.String(128), nullable=True))


def downgrade():
    with op.batch_alter_table("bot_users") as batch:
        batch.drop_column("full_name")
        batch.drop_column("username")
