"""Add randomized schedule state for rules."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0003_schedule_and_context"
down_revision = "0002_listing_search_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("watch_rules")}
    if "next_run_at" not in columns:
        op.add_column("watch_rules", sa.Column("next_run_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("watch_rules", "next_run_at")
