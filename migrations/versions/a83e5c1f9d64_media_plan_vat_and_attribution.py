"""Media plan settings a manager overrides: VAT of the budget, lead attribution."""

import sqlalchemy as sa
from alembic import op

revision = "a83e5c1f9d64"
down_revision = "f1c8d6b2a473"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("media_plans") as batch:
        batch.add_column(sa.Column("budget_vat", sa.String(10), nullable=True))
        batch.add_column(sa.Column("lead_attribution", sa.String(10), nullable=True))


def downgrade():
    with op.batch_alter_table("media_plans") as batch:
        batch.drop_column("lead_attribution")
        batch.drop_column("budget_vat")
