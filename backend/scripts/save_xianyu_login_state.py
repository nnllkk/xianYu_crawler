"""以可见浏览器完成用户本人登录，并保存 Playwright storage_state JSON。"""

import argparse
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import XianyuAccount, beijing_now  # noqa: E402
from app.services.xianyu_accounts import XianyuAccountService  # noqa: E402


async def save_login_state(name: str) -> Path:
    settings = get_settings()
    account_service = XianyuAccountService(settings)
    state_path = account_service.state_path_for(name)

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=False, channel="chrome")
        except Exception:
            browser = await playwright.chromium.launch(
                headless=False, executable_path=settings.playwright_executable_path
            )
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://www.goofish.com/", wait_until="domcontentloaded")
        input("请在打开的浏览器中完成闲鱼登录与平台要求的验证，确认首页可正常访问后按 Enter 保存登录状态：")
        await context.storage_state(path=str(state_path))
        await context.close()
        await browser.close()

    session = SessionLocal()
    try:
        account = session.query(XianyuAccount).filter_by(name=name).one_or_none()
        if account is None:
            account = XianyuAccount(name=name, state_path=str(state_path))
            session.add(account)
        account.state_path = str(state_path)
        account.status = "active"
        account.failure_count = 0
        account.last_error = None
        account.cooldown_until = None
        account.updated_at = beijing_now()
        session.commit()
    finally:
        session.close()
    return state_path


def main() -> int:
    parser = argparse.ArgumentParser(description="手动登录闲鱼并保存可复用的 storage_state JSON")
    parser.add_argument("name", help="账号别名，仅支持字母、数字、连字符和下划线")
    args = parser.parse_args()
    state_path = asyncio.run(save_login_state(args.name))
    print(f"登录状态已保存：{state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
