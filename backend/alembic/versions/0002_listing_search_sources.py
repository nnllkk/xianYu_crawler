"""Store the search queries that matched each listing."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0002_listing_search_sources"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "matched_search_queries" not in {column["name"] for column in inspect(op.get_bind()).get_columns("listings")}:
        op.add_column("listings", sa.Column("matched_search_queries", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("listings", "matched_search_queries")
