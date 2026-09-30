import asyncio
import logging
import threading
from hashlib import sha256
from datetime import datetime

from sqlalchemy import select
from pydantic import ValidationError

from ..config import Settings
from ..database import SessionLocal
from ..llm.service import LLMService
from ..llm.schemas import UserRequirement
from ..models import Listing, NotificationLog, NotificationState, TaskRun, WatchRule, beijing_now, to_beijing_naive
from .collector import AccessLimitedError, CollectionError, CollectorService, LoginRequiredError
from .filtering import deterministic_filter
from .mailer import Mailer
from .xianyu_accounts import XianyuAccountService

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
        self.accounts = XianyuAccountService(settings)

    def requirement_for(self, rule: WatchRule) -> UserRequirement:
        """复用创建规则时的解析结果；仅为历史或损坏缓存补做一次解析。"""
        if rule.parsed_requirement:
            try:
                return UserRequirement.model_validate(rule.parsed_requirement)
            except ValidationError:
                logger.warning("saved rule requirement is invalid; reparsing", extra={"rule_id": rule.id})
        requirement = self.llm.parse_requirement(rule.product, rule.extra_conditions, rule.budget)
        rule.parsed_requirement = requirement.model_dump(mode="json")
        return requirement

    @staticmethod
    def _llm_candidate(candidate: Listing, conditions: list[str] | None = None) -> dict[str, object]:
        """只传递搜索卡片实际提供的证据，避免提示词诱导模型补全详情页信息。"""
        return {
            "xianyu_item_id": candidate.xianyu_item_id,
            "price": float(candidate.price),
            "title": candidate.title,
            "raw_data": {"card_text": (candidate.raw_data or {}).get("card_text")},
            "seller_location": candidate.seller_location,
            "collected_at": candidate.collected_at.isoformat(),
            "conditions": conditions if conditions is not None else candidate.conditions or [],
        }

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
            task.status, task.stage, task.started_at = "running", "scraping", beijing_now()
            task.error_message, task.finished_at = None, None
            task.scraped_count, task.candidate_count, task.sent_count = 0, 0, 0
            session.commit()

            requirement = self.requirement_for(rule)
            # 数据库字段保留为 JSON 以兼容既有任务记录；新任务只保存这一条搜索语句。
            task.search_queries, task.stage = [requirement.search_query], "scraping"
            session.commit()

            # 采集和分析分离为两个协程：浏览器始终单线程顺序翻页，分析端通过
            # 有界队列消费已采集页面。LLM 的同步请求转入线程，避免阻塞分页等待。
            page_queue: asyncio.Queue[list] = asyncio.Queue(maxsize=self.settings.collector_page_queue_size)
            candidate_pool: list[Listing] = []
            seen_item_ids: set[str] = set()
            consumer_error: list[Exception] = []
            initial_assessment_limiter = asyncio.Semaphore(self.settings.initial_assessment_concurrency)

            async def rank_and_send() -> None:
                if not candidate_pool:
                    return
                self._raise_if_stopped(rule_id)
                task.stage = "ranking"
                session.commit()
                ranked_items = [self._llm_candidate(candidate) for candidate in candidate_pool]
                ranking = await asyncio.to_thread(self.llm.rank, requirement, ranked_items)
                selected = {item.xianyu_item_id: item for item in ranking.items if item.recommended}
                if selected:
                    task.stage = "sending"
                    body = ["闲鱼商品筛选结果", ""]
                    for candidate in candidate_pool:
                        result = selected.get(candidate.xianyu_item_id)
                        if result:
                            body.extend([f"价格：{candidate.price}", f"标题：{candidate.title}", f"链接：{candidate.url}",
                                         f"推荐理由：{result.reason}", f"风险：{'；'.join(result.risks) or '未发现明确风险'}", ""])
                    recipients = [recipient.email for recipient in rule.recipients]
                    await asyncio.to_thread(self.mailer.send, recipients, f"闲鱼筛选结果：{rule.product}", "\n".join(body))
                    for candidate in candidate_pool:
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
                    task.sent_count += len(selected)
                candidate_pool.clear()
                session.commit()

            async def process_page(raw_page: list) -> None:
                self._raise_if_stopped(rule_id)
                task.stage = "filtering"
                item_dicts = [{"xianyu_item_id": raw.xianyu_item_id, "title": raw.title, "price": raw.price,
                               "url": raw.url, "image_url": raw.image_url, "seller_location": raw.seller_location,
                               "matched_search_queries": raw.matched_search_queries, "raw_data": raw.raw_data,
                               "collected_at": to_beijing_naive(datetime.fromisoformat(raw.collected_at.replace("Z", "+00:00")))}
                              for raw in raw_page]
                task.scraped_count += len(item_dicts)
                candidates = deterministic_filter(session, rule, item_dicts, [recipient.email for recipient in rule.recipients])
                new_candidates: list[Listing] = []
                for candidate in candidates:
                    if candidate.xianyu_item_id in seen_item_ids:
                        continue
                    seen_item_ids.add(candidate.xianyu_item_id)
                    existing = session.scalar(select(Listing).where(Listing.xianyu_item_id == candidate.xianyu_item_id))
                    if existing:
                        # 商品卡片是快照；更新后可正确判断同商品降价时是否应再次通知。
                        for field in ("title", "price", "url", "image_url", "seller_location", "matched_search_queries", "raw_data", "collected_at"):
                            setattr(existing, field, getattr(candidate, field))
                        candidate = existing
                    else:
                        session.add(candidate)
                    session.flush()
                    new_candidates.append(candidate)

                async def assess_candidate(candidate: Listing) -> Listing | None:
                    # 商品信息提取与首次 LLM2 判断共用限流器，确保本地模型最多同时处理两件商品。
                    async with initial_assessment_limiter:
                        source_text = f"{candidate.title}\n{(candidate.raw_data or {}).get('card_text') or ''}"
                        text_hash = sha256(source_text.encode("utf-8")).hexdigest()
                        if candidate.conditions and candidate.extracted_text_hash == text_hash:
                            parsed_conditions = candidate.conditions
                        else:
                            parsed = await asyncio.to_thread(self.llm.parse_product, {
                                "xianyu_item_id": candidate.xianyu_item_id, "title": candidate.title,
                                "raw_data": candidate.raw_data,
                            })
                            parsed_conditions, candidate.extraction_status = parsed.conditions, parsed.extraction_status
                            candidate.conditions, candidate.extracted_text_hash = parsed_conditions, text_hash
                        assessment = await asyncio.to_thread(
                            self.llm.assess, requirement, self._llm_candidate(candidate, parsed_conditions),
                        )
                        return candidate if assessment.worthwhile else None

                task.stage = "analyzing"
                # gather 保持输入顺序，后续候选池和排序阶段仍只在当前协程修改数据库状态。
                assessed_candidates = await asyncio.gather(*(assess_candidate(candidate) for candidate in new_candidates))
                for candidate in assessed_candidates:
                    if candidate is None:
                        continue
                    candidate_pool.append(candidate)
                    task.candidate_count += 1
                    if len(candidate_pool) == self.settings.candidate_pool_size:
                        await rank_and_send()
                session.commit()

            async def consume_pages() -> None:
                while True:
                    raw_page = await page_queue.get()
                    try:
                        if raw_page is None:
                            return
                        if consumer_error:
                            continue
                        try:
                            await process_page(raw_page)
                        except Exception as exc:  # 继续消费队列，避免生产端在满队列上永久等待。
                            consumer_error.append(exc)
                    finally:
                        page_queue.task_done()

            async def on_page(raw_page: list) -> None:
                self._raise_if_stopped(rule_id)
                if consumer_error:
                    raise consumer_error[0]
                await page_queue.put(raw_page)

            consumer = asyncio.create_task(consume_pages())
            # 仅在登录态失效或平台明确拒绝当前会话时切换账号。
            # 页面结构变化、超时等普通错误不能进入冷却，否则会误伤可正常使用的账号。
            tried_account_ids: set[str] = set()
            collected = False
            while account := self.accounts.choose_available(session, tried_account_ids):
                tried_account_ids.add(account.id)
                task.xianyu_account_id = account.id
                session.commit()
                try:
                    if hasattr(self.collector, "search_pages"):
                        await self.collector.search_pages(requirement.search_query, account.state_path, on_page)
                    else:
                        # 兼容外部实现仍只提供 search() 的采集器；生产 CollectorService 走逐页接口。
                        await on_page(await self.collector.search(requirement.search_query, account.state_path))
                except LoginRequiredError as exc:
                    self.accounts.mark_login_required(account, str(exc))
                    session.commit()
                    logger.info("xianyu account needs login", extra={"account_id": account.id, "reason": str(exc)})
                    continue
                except AccessLimitedError as exc:
                    self.accounts.mark_access_limited(account, str(exc))
                    session.commit()
                    logger.warning("xianyu account entered cooldown", extra={"account_id": account.id, "reason": str(exc)})
                    continue
                except CollectionError:
                    # 普通采集故障保留原始异常，供任务记录和日志诊断；账号继续保持 active。
                    raise
                self.accounts.mark_success(account)
                session.commit()
                collected = True
                break
            await page_queue.put(None)
            await consumer
            if consumer_error:
                raise consumer_error[0]
            if not collected:
                raise RuntimeError("没有可用的闲鱼登录状态；请完成登录，或等待受限账号冷却后再试。")
            self._raise_if_stopped(rule_id)
            await rank_and_send()
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
            # 采集器抛出普通异常时，消费协程尚未收到结束标记；主动取消它，
            # 避免其继续使用即将关闭的数据库会话。
            consumer = locals().get("consumer")
            if consumer and not consumer.done():
                consumer.cancel()
                try:
                    await consumer
                except asyncio.CancelledError:
                    pass
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
