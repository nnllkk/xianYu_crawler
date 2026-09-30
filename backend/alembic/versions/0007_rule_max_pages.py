"""Store the page limit selected for each monitoring rule."""

import sqlalchemy as sa
from alembic import op


revision = "0007_rule_max_pages"
down_revision = "0006_remove_exclude_keywords"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A server default lets existing rules retain the former 20-page behavior.
    op.add_column(
        "watch_rules",
        sa.Column("max_pages", sa.Integer(), nullable=False, server_default="20"),
    )
    op.alter_column("watch_rules", "max_pages", server_default=None)


def downgrade() -> None:
    op.drop_column("watch_rules", "max_pages")
