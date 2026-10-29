"""Ratings of bot answers and model spend by person."""

import sqlalchemy as sa
from alembic import op

revision = "e7b3f1a0c925"
down_revision = "d2a7c94e1b58"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("app_mode", sa.String(20), nullable=False),
        sa.Column("chat_id", sa.String(30), nullable=False),
        sa.Column("user_id", sa.String(30), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("client_id", sa.String(32), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )
    with op.batch_alter_table("llm_usage") as batch:
        batch.add_column(sa.Column("user_id", sa.String(30), nullable=True))


def downgrade():
    with op.batch_alter_table("llm_usage") as batch:
        batch.drop_column("user_id")
    op.drop_table("feedback")
