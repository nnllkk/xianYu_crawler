"""Convert historical UTC DATETIME values to Beijing time."""

from alembic import op


revision = "0004_store_beijing_time"
down_revision = "0003_schedule_and_context"
branch_labels = None
depends_on = None


TIME_COLUMNS = {
    "watch_rules": ("next_run_at", "created_at", "updated_at"),
    "listings": ("collected_at", "updated_at"),
    "task_runs": ("started_at", "finished_at", "created_at"),
    "notification_states": ("last_sent_at",),
    "notification_logs": ("created_at",),
}


def _shift(minutes: int) -> None:
    # 旧版本将 UTC 写进无时区 DATETIME。逐列平移可保留所有历史事件的真实时刻。
    for table, columns in TIME_COLUMNS.items():
        for column in columns:
            op.execute(f"UPDATE `{table}` SET `{column}` = DATE_ADD(`{column}`, INTERVAL {minutes} MINUTE) "
                       f"WHERE `{column}` IS NOT NULL")


def upgrade() -> None:
    _shift(480)


def downgrade() -> None:
    _shift(-480)
