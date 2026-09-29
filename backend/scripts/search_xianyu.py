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


class SearchPageError(RuntimeError):
    """页面无法安全完成搜索或页面结构不符合预期时抛出。"""


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
        "--max-scrolls",
        type=int,
        default=100,
        help="加载更多商品时允许的最大滚动次数，默认 100。",
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
    body_text = await page.locator("body").inner_text()
    marker = next((item for item in BLOCKED_PAGE_MARKERS if item in body_text), None)
    if marker:
        raise SearchPageError(
            f"闲鱼页面未允许自动化访问，检测到：{marker}。"
            "请在可合法访问的浏览器会话中完成登录或验证后再重试。"
        )


async def search(page: Page, keyword: str) -> None:
    await page.goto(GOOFISH_HOME_URL, wait_until="domcontentloaded")
    await ensure_page_is_usable(page)

    search_input = await first_visible_locator(page, SEARCH_INPUT_SELECTORS)
    if search_input is None:
        raise SearchPageError("未找到闲鱼搜索框，页面结构可能已变化。")

    await search_input.fill(keyword)
    await search_input.press("Enter")
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(1_000)
    await ensure_page_is_usable(page)


async def scroll_to_end(page: Page, max_scrolls: int) -> None:
    """无限滚动列表没有总页数；连续三次高度不增长即视为已加载完当前结果。"""
    stable_rounds = 0
    previous_height = 0
    for _ in range(max_scrolls):
        height = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(700)
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height <= height and height <= previous_height:
            stable_rounds += 1
            if stable_rounds >= 3:
                return
        else:
            stable_rounds = 0
        previous_height = new_height


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
    for selector in CARD_SELECTORS:
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


async def collect_listings(page: Page, keyword: str, max_scrolls: int) -> list[Listing]:
    await scroll_to_end(page, max_scrolls)
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


async def run(keyword: str, headless: bool, max_scrolls: int,
              executable_path: str | None = None) -> list[Listing]:
    async with async_playwright() as playwright:
        executable_path = executable_path or os.environ.get("PLAYWRIGHT_EXECUTABLE_PATH")
        browser = await playwright.chromium.launch(headless=headless, executable_path=executable_path)
        page = await browser.new_page()
        try:
            await search(page, keyword)
            return await collect_listings(page, keyword, max_scrolls)
        finally:
            await browser.close()


async def run_queries(queries: list[str], headless: bool, max_scrolls: int,
                      executable_path: str | None = None,
                      delay_min_seconds: int = 5,
                      delay_max_seconds: int = 15,
                      storage_state_path: str | None = None) -> list[Listing]:
    """在一个浏览器上下文中串行执行多条搜索，降低短时间内建立多个会话的概率。"""
    if not queries:
        return []
    async with async_playwright() as playwright:
        executable_path = executable_path or os.environ.get("PLAYWRIGHT_EXECUTABLE_PATH")
        browser = await playwright.chromium.launch(headless=headless, executable_path=executable_path)
        context_kwargs = {"storage_state": storage_state_path} if storage_state_path else {}
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()
        merged: dict[str, Listing] = {}
        try:
            for index, query in enumerate(queries):
                await search(page, query)
                for item in await collect_listings(page, query, max_scrolls):
                    if not item.xianyu_item_id:
                        continue
                    existing = merged.get(item.xianyu_item_id)
                    if existing:
                        existing.matched_search_queries = sorted(set(existing.matched_search_queries + item.matched_search_queries))
                    else:
                        merged[item.xianyu_item_id] = item
                if index < len(queries) - 1:
                    await page.wait_for_timeout(random.randint(delay_min_seconds, delay_max_seconds) * 1000)
        finally:
            await context.close()
            await browser.close()
        return list(merged.values())


def main() -> int:
    args = parse_args()
    keyword = (args.keyword or input("请输入要搜索的商品： ")).strip()
    if not keyword:
        print("商品搜索词不能为空。", file=sys.stderr)
        return 2
    if args.max_scrolls < 1:
        print("--max-scrolls 必须大于 0。", file=sys.stderr)
        return 2

    try:
        listings = asyncio.run(run(keyword, args.headless, args.max_scrolls))
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
