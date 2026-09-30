#!/usr/bin/env python3
"""在闲鱼网页中搜索商品，并将当前加载的商品卡片输出为标准化 JSON。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, async_playwright


GOOFISH_HOME_URL = "https://www.goofish.com/"
BLOCKED_PAGE_MARKERS = ("非法访问", "请使用正常浏览器访问闲鱼", "验证码", "安全验证")
SEARCH_INPUT_SELECTORS = (
    'input[type="search"]',
    'input[type="text"]',
    'input[placeholder*="搜索"]',
    'input[placeholder*="搜"]',
)
CARD_SELECTORS = (
    'a[href*="/item?id="]',
    '[data-testid*="item"]',
    '[class*="item-card"]',
    '[class*="ItemCard"]',
    '[class*="feed-card"]',
    '[class*="FeedCard"]',
)
NEXT_PAGE_SELECTORS = (
    'button[class*="search-pagination-arrow-container"]:has([class*="search-pagination-arrow-right"])',
    'button[aria-label*="下一页"]',
    'a[aria-label*="下一页"]',
    '[role="button"][aria-label*="下一页"]',
    'button:has-text("下一页")',
    'a:has-text("下一页")',
    '[role="button"]:has-text("下一页")',
)


class SearchPageError(RuntimeError):
    """采集搜索页时发生的可预期错误的基类。"""


class LoginRequiredError(SearchPageError):
    """登录态已失效，页面跳转到闲鱼登录流程。"""


class AccessLimitedError(SearchPageError):
    """闲鱼明确返回了验证码、安全验证或非法访问页面。"""


class CollectionError(SearchPageError):
    """非登录、非平台限制的普通采集错误，例如页面结构变化。"""


@dataclass
class Listing:
    """后续去重、筛选和推送流程使用的最小商品标准结构。"""

    xianyu_item_id: str | None
    title: str | None
    price: float | None
    url: str | None
    image_url: str | None
    seller_name: str | None
    seller_location: str | None
    description: str | None
    source_keyword: str
    matched_search_queries: list[str]
    collected_at: str
    raw_data: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从闲鱼搜索页提取商品列表 JSON")
    parser.add_argument("keyword", nargs="?", help="商品搜索词；未提供时会在启动后询问")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="使用无头浏览器；默认显示浏览器窗口，便于人工完成登录或查看访问限制。",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=20,
        help="最多采集的搜索结果页数，默认 20。",
    )
    return parser.parse_args()


async def first_visible_locator(page: Page, selectors: tuple[str, ...]) -> Locator | None:
    """页面结构可能调整，按候选选择器寻找当前真正可用的元素。"""
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            # 闲鱼首页在 DOMContentLoaded 后异步挂载搜索框，需等待元素真正可交互。
            await locator.wait_for(state="visible", timeout=2_000)
        except PlaywrightTimeoutError:
            continue
        if await locator.count() and await locator.is_visible():
            return locator
    return None


async def ensure_page_is_usable(page: Page) -> None:
    page_url = page.url.lower()
    if "passport.goofish.com" in page_url or "mini_login" in page_url:
        raise LoginRequiredError(f"闲鱼登录态已失效，已跳转到登录页面：{page.url}")

    body_text = await page.locator("body").inner_text()
    marker = next((item for item in BLOCKED_PAGE_MARKERS if item in body_text), None)
    if marker:
        raise AccessLimitedError(
            f"闲鱼页面未允许自动化访问，检测到：{marker}。"
            "请在可合法访问的浏览器会话中完成登录或验证后再重试。"
        )


async def search(page: Page, keyword: str) -> None:
    await page.goto(GOOFISH_HOME_URL, wait_until="domcontentloaded")
    await ensure_page_is_usable(page)

    search_input = await first_visible_locator(page, SEARCH_INPUT_SELECTORS)
    if search_input is None:
        raise CollectionError("未找到闲鱼搜索框，页面结构可能已变化。")

    await search_input.fill(keyword)
    await search_input.press("Enter")
    await page.wait_for_load_state("domcontentloaded")
    await ensure_page_is_usable(page)


async def get_text(locator: Locator) -> str | None:
    if not await locator.count():
        return None
    value = (await locator.first.inner_text()).strip()
    return value or None


async def get_attribute(locator: Locator, attribute: str) -> str | None:
    if not await locator.count():
        return None
    value = await locator.first.get_attribute(attribute)
    return value.strip() if value else None


def parse_price(text: str | None) -> float | None:
    if not text:
        return None
    # 搜索页将货币符号与数字拆分为不同节点，inner_text 中会包含换行或空格。
    matched = re.search(r"(?:￥|¥)\s*([0-9][0-9,]*(?:\.[0-9]+)?)(万)?", text)
    if not matched:
        return None
    price = float(matched.group(1).replace(",", ""))
    return price * 10_000 if matched.group(2) else price


def extract_item_id(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("id", "itemId", "item_id"):
        if query.get(key):
            return unquote(query[key][0])
    return None


async def get_cards(page: Page) -> Locator:
    # 搜索页在 DOMContentLoaded 后才异步渲染商品卡片。不能只依赖固定等待，
    # 否则慢一次渲染会被静默记录成“成功但 0 条商品”。
    primary_cards = page.locator(CARD_SELECTORS[0])
    try:
        await primary_cards.first.wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeoutError:
        pass
    if await primary_cards.count():
        return primary_cards

    for selector in CARD_SELECTORS[1:]:
        cards = page.locator(selector)
        if await cards.count():
            return cards

    # 页面类名变化时，仍只保留链接到商品详情的节点，避免把导航链接误当商品。
    return page.locator('a[href*="item"], a[href*="detail"]')


async def normalize_card(card: Locator, keyword: str, collected_at: str) -> Listing | None:
    link = card.locator('a[href*="item"], a[href*="detail"]').first
    # 兜底选择器本身可能就是详情页链接，因此同时读取卡片自身和其子链接。
    url = await get_attribute(card, "href") or await get_attribute(link, "href")
    if url and url.startswith("/"):
        url = f"https://www.goofish.com{url}"

    text = await get_text(card)
    title = await get_text(card.locator('[class*="title"], [class*="Title"], h2, h3'))
    if title is None:
        title = (
            await get_attribute(card, "title")
            or await get_attribute(link, "title")
            or await get_text(link)
            or text
        )

    # 商品价格优先从价格节点读取；找不到时才从整个卡片文本中解析。
    price_text = await get_text(card.locator('[class*="price"], [class*="Price"]'))
    price = parse_price(price_text) or parse_price(text)
    image_url = await get_attribute(card.locator("img").first, "src")
    if image_url and image_url.startswith("//"):
        image_url = f"https:{image_url}"

    # 当前搜索卡片展示的是卖家地区，未展示稳定的卖家昵称，不能将地区误写为昵称。
    seller_name = None
    seller_location = (
        await get_attribute(card.locator('[class*="seller-text-wrap"]').first, "title")
        or await get_text(card.locator('[class*="location"], [class*="address"]'))
    )

    # 缺少商品链接和标题的节点通常不是商品卡片，不输出为商品数据。
    if not url or not title:
        return None

    return Listing(
        xianyu_item_id=extract_item_id(url),
        title=title,
        price=price,
        url=url,
        image_url=image_url,
        seller_name=seller_name,
        seller_location=seller_location,
        description=None,
        source_keyword=keyword,
        matched_search_queries=[keyword],
        collected_at=collected_at,
        raw_data={"card_text": text, "price_text": price_text},
    )


async def collect_listings(page: Page, keyword: str) -> list[Listing]:
    cards = await get_cards(page)
    # 标准化 JSON 对外明确携带北京时间偏移，避免下游将无时区时间误解为 UTC。
    collected_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    listings: list[Listing] = []
    seen_urls: set[str] = set()

    for index in range(await cards.count()):
        listing = await normalize_card(cards.nth(index), keyword, collected_at)
        if listing is None or listing.url in seen_urls:
            continue
        seen_urls.add(listing.url)
        listings.append(listing)
    return listings


async def find_next_page(page: Page) -> Locator | None:
    """返回当前可点击的下一页控件；页面没有分页或已到末页时返回 None。"""
    locator = await first_visible_locator(page, NEXT_PAGE_SELECTORS)
    if locator is None:
        return None
    classes = (await locator.get_attribute("class") or "").lower()
    disabled = (
        await locator.is_disabled()
        or (await locator.get_attribute("aria-disabled")) == "true"
        or "disabled" in classes
    )
    return None if disabled else locator


async def pause_between_pages(page: Page, minimum_seconds: int, maximum_seconds: int) -> None:
    """翻页前后留出随机人工操作间隔，避免对搜索结果连续发出翻页请求。"""
    await page.wait_for_timeout(random.randint(minimum_seconds, maximum_seconds) * 1000)


async def wait_for_next_page_results(page: Page, previous_first_href: str | None) -> None:
    """等待分页结果换页，避免下一轮重复采集仍留在 DOM 中的上一页商品。"""
    try:
        await page.wait_for_function(
            """previousHref => {
                const firstCard = document.querySelector("a[href*='/item?id=']");
                return Boolean(firstCard && firstCard.getAttribute('href') !== previousHref);
            }""",
            arg=previous_first_href,
            timeout=10_000,
        )
    except PlaywrightTimeoutError as exc:
        raise CollectionError("翻页后未等到新的商品结果，页面可能未完成更新。") from exc


async def launch_collector_browser(playwright: Any, headless: bool, executable_path: str | None) -> Any:
    """优先复用本机 Chrome，保持登录采集和搜索采集处于同一浏览器通道。"""
    if executable_path:
        return await playwright.chromium.launch(headless=headless, executable_path=executable_path)

    try:
        return await playwright.chromium.launch(headless=headless, channel="chrome")
    except Exception as chrome_error:
        # Docker、CI 等环境常没有系统 Chrome，回退到 Playwright 自带 Chromium 保持可运行。
        print(f"本机 Chrome 启动失败，回退到 Playwright Chromium：{chrome_error}")
        return await playwright.chromium.launch(headless=headless)


async def _run_query(query: str, headless: bool, max_pages: int,
                     executable_path: str | None = None,
                     delay_min_seconds: int = 5,
                     delay_max_seconds: int = 15,
                     storage_state_path: str | None = None,
                     on_page: Callable[[list[Listing]], Awaitable[None]] | None = None) -> list[Listing]:
    """搜索一次，并在同一结果集内按页采集，直到末页或达到页数上限。"""
    if not query:
        return []
    if max_pages < 1:
        raise ValueError("max_pages 必须大于 0")
    if delay_min_seconds < 0 or delay_max_seconds < delay_min_seconds:
        raise ValueError("翻页等待时间配置无效")
    async with async_playwright() as playwright:
        executable_path = executable_path or os.environ.get("PLAYWRIGHT_EXECUTABLE_PATH")
        browser = await launch_collector_browser(playwright, headless, executable_path)
        context_kwargs = {"storage_state": storage_state_path} if storage_state_path else {}
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()
        merged: dict[str, Listing] = {}
        try:
            await search(page, query)
            for page_number in range(max_pages):
                page_items = await collect_listings(page, query)
                if on_page:
                    await on_page(page_items)
                for item in page_items:
                    if item.xianyu_item_id:
                        merged.setdefault(item.xianyu_item_id, item)
                if page_number == max_pages - 1:
                    break
                next_page = await find_next_page(page)
                if next_page is None:
                    break
                # 闲鱼将分页控件放在结果页底部，必须先滚动到可视区域才能稳定点击。
                await next_page.scroll_into_view_if_needed()
                await pause_between_pages(page, delay_min_seconds, delay_max_seconds)
                previous_first_href = await get_attribute(
                    (await get_cards(page)).first,
                    "href",
                )
                await next_page.click()
                await page.wait_for_load_state("domcontentloaded")
                await wait_for_next_page_results(page, previous_first_href)
                await ensure_page_is_usable(page)
                await pause_between_pages(page, delay_min_seconds, delay_max_seconds)
        finally:
            await context.close()
            await browser.close()
        return list(merged.values())


async def run_query(query: str, headless: bool, max_pages: int,
                    executable_path: str | None = None,
                    delay_min_seconds: int = 5,
                    delay_max_seconds: int = 15,
                    storage_state_path: str | None = None,
                    on_page: Callable[[list[Listing]], Awaitable[None]] | None = None) -> list[Listing]:
    """以三类业务异常向上层暴露采集结果，避免普通故障误触发账号冷却。"""
    try:
        return await _run_query(
            query,
            headless,
            max_pages,
            executable_path,
            delay_min_seconds,
            delay_max_seconds,
            storage_state_path,
            on_page,
        )
    except (LoginRequiredError, AccessLimitedError, CollectionError):
        raise
    except Exception as exc:
        raise CollectionError(f"闲鱼采集发生普通异常：{type(exc).__name__}: {exc}") from exc


def main() -> int:
    args = parse_args()
    keyword = (args.keyword or input("请输入要搜索的商品： ")).strip()
    if not keyword:
        print("商品搜索词不能为空。", file=sys.stderr)
        return 2
    if args.max_pages < 1:
        print("--max-pages 必须大于 0。", file=sys.stderr)
        return 2

    try:
        listings = asyncio.run(run_query(keyword, args.headless, args.max_pages))
    except SearchPageError as exc:
        print(f"采集失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # 保留浏览器或页面异常的真实信息，便于适配页面结构。
        print(f"采集失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(json.dumps([asdict(listing) for listing in listings], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
