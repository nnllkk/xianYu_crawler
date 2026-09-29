#!/usr/bin/env python3
"""诊断闲鱼 storage_state 是否能在采集器浏览器环境中通过首页和搜索页校验。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings  # noqa: E402
from search_xianyu import (  # noqa: E402
    BLOCKED_PAGE_MARKERS,
    GOOFISH_HOME_URL,
    SEARCH_INPUT_SELECTORS,
    first_visible_locator,
)


AUTH_COOKIE_NAMES = {"unb", "tracknick", "lgc"}


def inspect_state_file(path: Path) -> dict[str, Any]:
    """读取登录状态的结构和必要标识，但绝不输出 Cookie 内容。"""
    state = json.loads(path.read_text(encoding="utf-8"))
    cookies = state.get("cookies")
    if not isinstance(cookies, list):
        raise ValueError("storage_state 缺少 cookies 数组")
    cookie_names = {cookie.get("name") for cookie in cookies if isinstance(cookie, dict)}
    return {
        "state_file": path.name,
        "cookie_count": len(cookies),
        "origin_count": len(state.get("origins") or []),
        "login_cookie_names": sorted(AUTH_COOKIE_NAMES.intersection(cookie_names)),
    }


def blocked_marker(body_text: str) -> str | None:
    return next((marker for marker in BLOCKED_PAGE_MARKERS if marker in body_text), None)


async def verify_state(path: Path, query: str | None, headed: bool) -> dict[str, Any]:
    settings = get_settings()
    result = inspect_state_file(path)
    result["browser_mode"] = "headed" if headed else "headless"

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=not headed,
            executable_path=settings.playwright_executable_path,
        )
        context = await browser.new_context(storage_state=str(path))
        page = await context.new_page()
        try:
            response = await page.goto(GOOFISH_HOME_URL, wait_until="domcontentloaded")
            result["home_http_status"] = response.status if response else None
            home_marker = blocked_marker(await page.locator("body").inner_text())
            result["home_blocked_marker"] = home_marker
            runtime_cookie_names = {cookie["name"] for cookie in await context.cookies(GOOFISH_HOME_URL)}
            result["runtime_login_cookie_names"] = sorted(AUTH_COOKIE_NAMES.intersection(runtime_cookie_names))

            if home_marker or not query:
                return result

            search_input = await first_visible_locator(page, SEARCH_INPUT_SELECTORS)
            result["search_input_found"] = search_input is not None
            if search_input is None:
                return result
            await search_input.fill(query)
            await search_input.press("Enter")
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(1_000)
            result["search_blocked_marker"] = blocked_marker(await page.locator("body").inner_text())
            return result
        finally:
            await context.close()
            await browser.close()


def passed(result: dict[str, Any], query: str | None) -> bool:
    if not result["login_cookie_names"] or result["home_blocked_marker"]:
        return False
    if query:
        return result.get("search_input_found") is True and not result.get("search_blocked_marker")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="验证闲鱼登录状态 JSON 是否可供 Playwright 采集器使用")
    parser.add_argument("account", help="账号别名，例如 account-1")
    parser.add_argument("--query", help="可选：同时验证一条真实搜索语句")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口，便于与无头采集结果对比")
    args = parser.parse_args()

    path = Path(get_settings().xianyu_state_dir) / f"{args.account}.json"
    if not path.is_file():
        print(json.dumps({"state_file": path.name, "error": "状态文件不存在"}, ensure_ascii=False, indent=2))
        return 2
    try:
        result = asyncio.run(verify_state(path, args.query, args.headed))
    except Exception as exc:
        print(json.dumps({"state_file": path.name, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
        return 2

    result["passed"] = passed(result, args.query)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
