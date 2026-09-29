"""Add locally managed Xianyu storage-state account metadata."""

from alembic import op
import sqlalchemy as sa


revision = "0005_xianyu_accounts"
down_revision = "0004_store_beijing_time"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "xianyu_accounts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False, unique=True),
        sa.Column("state_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("cooldown_until", sa.DateTime()),
        sa.Column("last_used_at", sa.DateTime()),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.add_column("task_runs", sa.Column("xianyu_account_id", sa.String(length=36), nullable=True))
    op.create_foreign_key("fk_task_runs_xianyu_account", "task_runs", "xianyu_accounts", ["xianyu_account_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint("fk_task_runs_xianyu_account", "task_runs", type_="foreignkey")
    op.drop_column("task_runs", "xianyu_account_id")
    op.drop_table("xianyu_accounts")
