"""Metrica goals a client's media plan counts as leads."""

import sqlalchemy as sa
from alembic import op

revision = "f1c8d6b2a473"
down_revision = "e7b3f1a0c925"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("media_plans") as batch:
        batch.add_column(sa.Column("lead_goal_ids", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("media_plans") as batch:
        batch.drop_column("lead_goal_ids")
