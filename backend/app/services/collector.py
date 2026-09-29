import sys
from pathlib import Path

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

    async def search(self, query: str, storage_state_path: str) -> list[RawListing]:
        return await run_query(query, self.settings.collector_headless, 20, self.settings.playwright_executable_path,
                               self.settings.page_delay_min_seconds,
                               self.settings.page_delay_max_seconds, storage_state_path)
