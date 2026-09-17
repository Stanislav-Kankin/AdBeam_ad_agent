"""Cache analytics snapshots and persist conversational context."""

import sqlalchemy as sa
from alembic import op

revision = "d1840f2e917b"
down_revision = "c8235c1d2f04"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "snapshot_cache",
        sa.Column("app_mode", sa.String(20), primary_key=True),
        sa.Column("client_id", sa.String(32), primary_key=True),
        sa.Column("period_start", sa.String(10), primary_key=True),
        sa.Column("period_end", sa.String(10), primary_key=True),
        sa.Column("quality", sa.String(10), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_snapshot_cache_expires_at", "snapshot_cache", ["expires_at"])
    op.create_table(
        "conversation_states",
        sa.Column("app_mode", sa.String(20), primary_key=True),
        sa.Column("chat_id", sa.String(30), primary_key=True),
        sa.Column("user_id", sa.String(30), primary_key=True),
        sa.Column("messages", sa.JSON(), nullable=False),
        sa.Column("active_client_id", sa.String(32), nullable=True),
        sa.Column("period", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_conversation_states_updated_at", "conversation_states", ["updated_at"])


def downgrade():
    op.drop_index("ix_conversation_states_updated_at", table_name="conversation_states")
    op.drop_table("conversation_states")
    op.drop_index("ix_snapshot_cache_expires_at", table_name="snapshot_cache")
    op.drop_table("snapshot_cache")
