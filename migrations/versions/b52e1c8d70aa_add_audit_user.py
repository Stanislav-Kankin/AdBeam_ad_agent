"""Record the initiating Telegram user; scheduled runs have no user."""

import sqlalchemy as sa
from alembic import op

revision = "b52e1c8d70aa"
down_revision = "ad077c7f4012"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("check_runs", "tool_events"):
        op.add_column(table, sa.Column("user_id", sa.String(30), nullable=True))
        op.create_index(f"ix_{table}_user_id", table, ["user_id"])


def downgrade():
    for table in ("check_runs", "tool_events"):
        op.drop_index(f"ix_{table}_user_id", table_name=table)
        op.drop_column(table, "user_id")
