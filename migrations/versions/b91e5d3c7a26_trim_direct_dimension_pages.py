"""Drop full Direct dimension pages: the warehouse now keeps the top rows of a day."""

from alembic import op

revision = "b91e5d3c7a26"
down_revision = "e8f4a2b6c913"
branch_labels = None
depends_on = None


def upgrade():
    # Every page of every search-query and placement report took gigabytes while
    # only the first 500 rows of a day were read. The warehouse refills the slim
    # version in the background; the daily purge job compacts the file.
    op.execute("DELETE FROM direct_dimension_pages")


def downgrade():
    pass
