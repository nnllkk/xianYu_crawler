import sys
from pathlib import Path
from collections.abc import Awaitable, Callable

from ..config import Settings

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))
from search_xianyu import Listing as RawListing  # noqa: E402
from search_xianyu import AccessLimitedError, CollectionError, LoginRequiredError, SearchPageError  # noqa: E402
from search_xianyu import run_query  # noqa: E402


class CollectorService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def search(self, query: str, storage_state_path: str, max_pages: int) -> list[RawListing]:
        return await run_query(query, self.settings.collector_headless, max_pages, self.settings.playwright_executable_path,
                               self.settings.page_delay_min_seconds,
                               self.settings.page_delay_max_seconds, storage_state_path)

    async def search_pages(self, query: str, storage_state_path: str,
                           on_page: Callable[[list[RawListing]], Awaitable[None]], max_pages: int) -> list[RawListing]:
        """每页采集完成即交给调用方，避免等待全部页面后才开始分析。"""
        return await run_query(query, self.settings.collector_headless, max_pages, self.settings.playwright_executable_path,
                               self.settings.page_delay_min_seconds,
                               self.settings.page_delay_max_seconds, storage_state_path, on_page)
