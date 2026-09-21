"""Invalidate snapshots created with incorrect Direct zero conversions."""

from alembic import op

revision = "fa7d31c08b42"
down_revision = "f30c9c2ae271"
branch_labels = None
depends_on = None


def upgrade():
    # Previously Direct's `--` goal value was stored as unknown instead of zero.
    # The original token cannot be reconstructed from cached JSON, so refetch it.
    op.execute("DELETE FROM snapshot_cache")
    op.execute("DELETE FROM daily_snapshots")
    op.execute("DELETE FROM direct_dimension_pages")


def downgrade():
    pass
