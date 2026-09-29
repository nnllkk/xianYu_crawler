from datetime import timedelta
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import XianyuAccount, beijing_now


class XianyuAccountService:
    """管理账号元数据；storage_state 文件不会进入 MySQL 或日志。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def state_path_for(self, name: str) -> Path:
        if not name or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in name):
            raise ValueError("账号名称仅支持字母、数字、连字符和下划线")
        directory = Path(self.settings.xianyu_state_dir)
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{name}.json"

    def choose_available(self, session: Session, excluded_ids: set[str]) -> XianyuAccount | None:
        now = beijing_now()
        statement = (select(XianyuAccount)
                     .where(XianyuAccount.is_enabled.is_(True), XianyuAccount.status == "active",
                            or_(XianyuAccount.cooldown_until.is_(None), XianyuAccount.cooldown_until <= now))
                     .order_by(XianyuAccount.last_used_at.is_(None).desc(), XianyuAccount.last_used_at.asc()))
        for account in session.scalars(statement):
            if account.id not in excluded_ids and Path(account.state_path).is_file():
                return account
        return None

    def mark_success(self, account: XianyuAccount) -> None:
        account.last_used_at = beijing_now()
        account.failure_count = 0
        account.last_error = None

    def mark_access_limited(self, account: XianyuAccount, reason: str) -> None:
        account.failure_count += 1
        account.last_error = reason
        account.last_used_at = beijing_now()
        account.status = "cooldown"
        account.cooldown_until = beijing_now() + timedelta(minutes=self.settings.xianyu_account_cooldown_minutes)
