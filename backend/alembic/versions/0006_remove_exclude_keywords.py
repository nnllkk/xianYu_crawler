"""Remove the dedicated exclusion-keyword rule field."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "0006_remove_exclude_keywords"
down_revision = "0005_xianyu_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("watch_rules")}
    if "exclude_keywords" in columns:
        op.drop_column("watch_rules", "exclude_keywords")


def downgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("watch_rules")}
    if "exclude_keywords" not in columns:
        op.add_column("watch_rules", sa.Column("exclude_keywords", sa.JSON(), nullable=True))
