"""Risks a specialist hid for a while."""

import sqlalchemy as sa
from alembic import op

revision = "d2a7c94e1b58"
down_revision = "c64f0a8d2e17"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "snoozed_risks",
        sa.Column("app_mode", sa.String(20), primary_key=True),
        sa.Column("client_id", sa.String(32), primary_key=True),
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("until", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("created_by", sa.String(30), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("snoozed_risks")
