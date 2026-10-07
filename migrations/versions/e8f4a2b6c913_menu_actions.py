"""Menu buttons survive a bot restart: their actions are kept in the database."""

import sqlalchemy as sa
from alembic import op

revision = "e8f4a2b6c913"
down_revision = "d5b2c7e91a40"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "menu_actions",
        sa.Column("token", sa.String(16), primary_key=True),
        sa.Column("app_mode", sa.String(20), nullable=False),
        sa.Column("chat_id", sa.String(30), nullable=False),
        sa.Column("user_id", sa.String(30), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("kwargs", sa.JSON(), nullable=False),
        sa.Column("screen_id", sa.String(8), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )


def downgrade():
    op.drop_table("menu_actions")
