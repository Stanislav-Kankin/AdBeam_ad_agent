"""Remember whether a client's main goals were chosen by a person or taken from campaigns."""

import sqlalchemy as sa
from alembic import op

revision = "b7d3f05a9e21"
down_revision = "a4c9e2f17b30"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("client_preferences") as batch:
        batch.add_column(sa.Column("goals_source", sa.String(20), nullable=True))


def downgrade():
    with op.batch_alter_table("client_preferences") as batch:
        batch.drop_column("goals_source")
