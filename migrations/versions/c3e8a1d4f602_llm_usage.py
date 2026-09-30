"""Token usage and estimated cost of every model call, for /balance."""

import sqlalchemy as sa
from alembic import op

revision = "c3e8a1d4f602"
down_revision = "b7d3f05a9e21"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("app_mode", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("model", sa.String(60), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
    )


def downgrade():
    op.drop_table("llm_usage")
