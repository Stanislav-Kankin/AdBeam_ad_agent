"""Persist client counters, goals and the Metrica catalog."""

import sqlalchemy as sa
from alembic import op

revision = "c8235c1d2f04"
down_revision = "b52e1c8d70aa"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "client_preferences",
        sa.Column("app_mode", sa.String(20), primary_key=True),
        sa.Column("client_id", sa.String(32), primary_key=True),
        sa.Column("client_login", sa.String(100), nullable=False),
        sa.Column("selected_counter_ids", sa.JSON(), nullable=False),
        sa.Column("primary_goal_ids", sa.JSON(), nullable=False),
        sa.Column("goal_roles", sa.JSON(), nullable=False),
        sa.Column("updated_by", sa.String(30), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_client_preferences_client_login", "client_preferences", ["client_login"])
    op.create_table(
        "metrica_counter_catalog",
        sa.Column("app_mode", sa.String(20), primary_key=True),
        sa.Column("counter_id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("site", sa.String(300), nullable=False),
        sa.Column("permission", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("goals", sa.JSON(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "client_counters",
        sa.Column("app_mode", sa.String(20), primary_key=True),
        sa.Column("client_id", sa.String(32), primary_key=True),
        sa.Column("counter_id", sa.Integer(), primary_key=True),
        sa.Column("linked", sa.Boolean(), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("client_counters")
    op.drop_table("metrica_counter_catalog")
    op.drop_index("ix_client_preferences_client_login", table_name="client_preferences")
    op.drop_table("client_preferences")
