from datetime import timedelta
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from playwright.async_api import async_playwright

from ..config import Settings
from ..database import SessionLocal
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

    async def capture_login_state(self, account_id: str) -> None:
        """打开可见浏览器，检测登录完成后自动保存 storage_state。"""
        session = SessionLocal()
        account = session.get(XianyuAccount, account_id)
        if not account:
            session.close()
            return
        state_path = Path(account.state_path)
        account.status, account.last_error = "login_pending", None
        session.commit()
        session.close()
        try:
            async with async_playwright() as playwright:
                browser = None
                try:
                    # Playwright 的 chrome channel 会自动定位本机 Chrome；失败才回退到其管理的 Chromium。
                    browser = await playwright.chromium.launch(headless=False, channel="chrome")
                except Exception:
                    browser = await playwright.chromium.launch(
                        headless=False, executable_path=self.settings.playwright_executable_path
                    )
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto("https://www.goofish.com/", wait_until="domcontentloaded")
                for _ in range(180):
                    await page.wait_for_timeout(2_000)
                    cookies = await context.cookies()
                    body = await page.locator("body").inner_text()
                    cookie_names = {cookie["name"] for cookie in cookies}
                    # 匿名访问也会产生 Cookie；要求出现淘宝账号标识 Cookie，避免把匿名状态误存为已登录。
                    authenticated = bool(cookie_names.intersection({"unb", "tracknick", "lgc"}))
                    blocked = any(marker in body for marker in ("非法访问", "安全验证", "验证码"))
                    if authenticated and not blocked:
                        await context.storage_state(path=str(state_path))
                        session = SessionLocal()
                        account = session.get(XianyuAccount, account_id)
                        if account:
                            account.status, account.failure_count, account.last_error = "active", 0, None
                            account.cooldown_until = None
                            session.commit()
                        session.close()
                        return
                await browser.close()
                raise RuntimeError("等待登录超时，请重新添加或重试该账号")
        except Exception as exc:
            session = SessionLocal()
            account = session.get(XianyuAccount, account_id)
            if account:
                account.status, account.last_error = "needs_login", str(exc)
                session.commit()
            session.close()
