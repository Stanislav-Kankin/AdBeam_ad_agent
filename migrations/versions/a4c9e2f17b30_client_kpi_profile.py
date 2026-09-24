"""Persist the client KPI profile and drop period snapshots built with empty-day nulls."""

import sqlalchemy as sa
from alembic import op

revision = "a4c9e2f17b30"
down_revision = "fb8a42d19c53"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("client_preferences") as batch:
        batch.add_column(sa.Column("targets", sa.JSON(), nullable=True))
    # Multi-day snapshots combined from daily rows lost all totals when one day had no
    # impressions. Daily rows are correct; only the derived period cache is rebuilt.
    op.execute("DELETE FROM snapshot_cache WHERE quality IN ('full', 'quick')")


def downgrade():
    with op.batch_alter_table("client_preferences") as batch:
        batch.drop_column("targets")
