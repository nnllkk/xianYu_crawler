import logging
import threading
from hashlib import sha256
from datetime import datetime

from sqlalchemy import select

from ..config import Settings
from ..database import SessionLocal
from ..llm.service import LLMService
from ..models import Listing, NotificationLog, NotificationState, TaskRun, WatchRule, beijing_now, to_beijing_naive
from .collector import CollectorService
from .filtering import deterministic_filter
from .mailer import Mailer

logger = logging.getLogger(__name__)


class TaskStoppedError(RuntimeError):
    """用户主动停止任务，不应被任务级重试再次启动。"""


class TaskRunner:
    _running: set[str] = set()
    _stop_requested: set[str] = set()
    _state_lock = threading.Lock()

    def __init__(self, settings: Settings, llm: LLMService) -> None:
        self.settings = settings
        self.llm = llm
        self.collector = CollectorService(settings)
        self.mailer = Mailer(settings)
    async def run(self, rule_id: str, task_id: str | None = None) -> str:
        with self._state_lock:
            if rule_id in self._stop_requested:
                raise TaskStoppedError("任务已被用户停止")
            if rule_id in self._running:
                raise RuntimeError("同一规则已有任务正在执行")
            self._running.add(rule_id)
            self._stop_requested.discard(rule_id)
        session = SessionLocal()
        task = None
        try:
            rule = session.get(WatchRule, rule_id)
            if not rule:
                raise ValueError("规则不存在")
            task = session.get(TaskRun, task_id) if task_id else TaskRun(rule_id=rule_id)
            if not task_id:
                session.add(task)
            task.status, task.stage, task.started_at = "running", "parsing", beijing_now()
            task.error_message, task.finished_at = None, None
            task.scraped_count, task.candidate_count, task.sent_count = 0, 0, 0
            session.commit()

            requirement = self.llm.parse_requirement(rule.product, rule.extra_conditions, rule.budget, rule.exclude_keywords or [])
            rule.parsed_requirement = requirement.model_dump(mode="json")
            task.search_queries, task.stage = requirement.search_queries, "scraping"
            session.commit()

            raw_items = await self.collector.search(requirement.search_queries)
            self._raise_if_stopped(rule_id)
            task.scraped_count = len(raw_items)
            session.commit()
            item_dicts = []
            for raw in raw_items:
                item_dicts.append({"xianyu_item_id": raw.xianyu_item_id, "title": raw.title, "price": raw.price,
                                   "url": raw.url, "image_url": raw.image_url, "seller_location": raw.seller_location,
                                   "matched_search_queries": raw.matched_search_queries,
                                   "raw_data": raw.raw_data,
                                   "collected_at": to_beijing_naive(datetime.fromisoformat(raw.collected_at.replace("Z", "+00:00")))})

            task.stage = "filtering"
            candidates = deterministic_filter(session, rule, item_dicts, [recipient.email for recipient in rule.recipients])
            for candidate in candidates:
                self._raise_if_stopped(rule_id)
                existing = session.scalar(select(Listing).where(Listing.xianyu_item_id == candidate.xianyu_item_id))
                if existing:
                    # 商品价格和卡片信息是快照，必须用本轮数据覆盖旧值，否则降价重推会失效。
                    existing.title = candidate.title
                    existing.price = candidate.price
                    existing.url = candidate.url
                    existing.image_url = candidate.image_url
                    existing.seller_location = candidate.seller_location
                    existing.matched_search_queries = candidate.matched_search_queries
                    existing.raw_data = candidate.raw_data
                    existing.collected_at = candidate.collected_at
                    candidates[candidates.index(candidate)] = existing
                else:
                    session.add(candidate)
                session.flush()
            task.candidate_count = len(candidates)
            session.commit()

            task.stage = "parsing"
            llm_candidates = []
            for candidate in candidates:
                source_text = f"{candidate.title}\n{(candidate.raw_data or {}).get('card_text') or ''}"
                text_hash = sha256(source_text.encode("utf-8")).hexdigest()
                if candidate.conditions and candidate.extracted_text_hash == text_hash:
                    parsed_conditions = candidate.conditions
                else:
                    parsed = self.llm.parse_product({"xianyu_item_id": candidate.xianyu_item_id,
                                                     "title": candidate.title, "raw_data": candidate.raw_data})
                    parsed_conditions, candidate.extraction_status = parsed.conditions, parsed.extraction_status
                    candidate.conditions = parsed_conditions
                    candidate.extracted_text_hash = text_hash
                llm_candidates.append({"xianyu_item_id": candidate.xianyu_item_id, "price": float(candidate.price),
                                       "title": candidate.title, "raw_data": {"card_text": (candidate.raw_data or {}).get("card_text")},
                                       "seller_location": candidate.seller_location, "collected_at": candidate.collected_at.isoformat(),
                                       "conditions": parsed_conditions})
            session.commit()

            task.stage = "ranking"
            # 排序调用可能较慢，先提交阶段信息让前端能显示真实进度。
            session.commit()
            ranking = self.llm.rank(requirement, llm_candidates)
            selected = {item.xianyu_item_id: item for item in ranking.items if item.recommended}
            if selected:
                task.stage = "sending"
                body = ["闲鱼商品筛选结果", ""]
                for candidate in candidates:
                    result = selected.get(candidate.xianyu_item_id)
                    if result:
                        body.extend([f"价格：{candidate.price}", f"标题：{candidate.title}", f"链接：{candidate.url}",
                                     f"推荐理由：{result.reason}", f"风险：{'；'.join(result.risks) or '未发现明确风险'}", ""])
                recipients = [recipient.email for recipient in rule.recipients]
                self.mailer.send(recipients, f"闲鱼筛选结果：{rule.product}", "\n".join(body))
                for candidate in candidates:
                    if candidate.xianyu_item_id not in selected:
                        continue
                    for email in recipients:
                        session.add(NotificationLog(task_run_id=task.id, rule_id=rule.id, listing_id=candidate.id,
                                                     email=email, price=candidate.price, status="sent"))
                        state = session.scalar(select(NotificationState).where(
                            NotificationState.rule_id == rule.id, NotificationState.listing_id == candidate.id,
                            NotificationState.email == email))
                        if state:
                            state.last_sent_price, state.last_sent_at = candidate.price, beijing_now()
                        else:
                            session.add(NotificationState(rule_id=rule.id, listing_id=candidate.id, email=email,
                                                          last_sent_price=candidate.price))
                task.sent_count = len(selected)
            task.status, task.stage, task.finished_at = "success", "done", beijing_now()
            session.commit()
            return task.id
        except Exception as exc:
            logger.exception("task failed", extra={"rule_id": rule_id, "task_id": task_id})
            if task:
                stopped = isinstance(exc, TaskStoppedError)
                task.status, task.stage, task.error_message, task.finished_at = (
                    "stopped" if stopped else "failed",
                    "stopped" if stopped else "failed",
                    str(exc),
                    beijing_now(),
                )
                session.commit()
            raise
        finally:
            session.close()
            with self._state_lock:
                self._running.discard(rule_id)
                self._stop_requested.discard(rule_id)

    @classmethod
    def request_stop(cls, rule_id: str) -> None:
        cls._stop_requested.add(rule_id)

    @classmethod
    def _raise_if_stopped(cls, rule_id: str) -> None:
        if rule_id in cls._stop_requested:
            raise TaskStoppedError("任务已被用户停止")
