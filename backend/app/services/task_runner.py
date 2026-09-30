import asyncio
import logging
import random
import threading
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
    ASSESSMENT_PARALLEL_THRESHOLD = 0.75
    COLLECTION_PAUSE_THRESHOLD = 0.80
    COLLECTION_RESUME_THRESHOLD = 0.20
    RANKING_START_THRESHOLD = 0.70
    RANKING_BATCH_SIZE = 20
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
    def _llm_candidate(candidate: Listing) -> dict[str, object]:
        """只传递搜索卡片实际提供的证据，避免提示词诱导模型补全详情页信息。"""
        return {
            "xianyu_item_id": candidate.xianyu_item_id,
            "price": float(candidate.price),
            "title": candidate.title,
            "raw_data": {"card_text": (candidate.raw_data or {}).get("card_text")},
            "seller_location": candidate.seller_location,
            "collected_at": candidate.collected_at.isoformat(),
        }

    @classmethod
    def assessment_batch_size(cls, queue_size: int, queue_capacity: int, max_concurrency: int) -> int:
        """仅在原始商品队列超过高水位时扩展初筛并发，避免本地模型空闲时仍争抢资源。"""
        if queue_size > queue_capacity * cls.ASSESSMENT_PARALLEL_THRESHOLD:
            return max_concurrency
        return 1

    @classmethod
    def should_pause_collection(cls, queue_size: int, queue_capacity: int) -> bool:
        """到达高水位后由回调阻塞浏览器翻页，直到消费端显式恢复。"""
        return queue_size >= queue_capacity * cls.COLLECTION_PAUSE_THRESHOLD

    @classmethod
    def is_collection_queue_low(cls, queue_size: int, queue_capacity: int) -> bool:
        return queue_size <= queue_capacity * cls.COLLECTION_RESUME_THRESHOLD

    @classmethod
    def should_start_ranking(cls, candidate_count: int, candidate_capacity: int) -> bool:
        """候选池超过 70% 后暂停初筛，优先释放候选池。"""
        return candidate_count > candidate_capacity * cls.RANKING_START_THRESHOLD

    @classmethod
    def is_candidate_pool_low(cls, candidate_count: int, candidate_capacity: int) -> bool:
        return candidate_count <= candidate_capacity * cls.COLLECTION_RESUME_THRESHOLD

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
            task.status, task.stage, task.started_at = "running", "running", beijing_now()
            task.error_message, task.finished_at = None, None
            task.scraped_count, task.candidate_count, task.sent_count = 0, 0, 0
            session.commit()

            requirement = self.requirement_for(rule)
            # 数据库字段保留为 JSON 以兼容既有任务记录；新任务只保存这一条搜索语句。
            task.search_queries, task.stage = [requirement.search_query], "running"
            session.commit()

            # 队列按商品而非页面计数，才可以对浏览器翻页实施稳定的高、低水位控制。
            # LLM 是同步客户端，放进线程执行，避免阻塞浏览器等待与队列恢复。
            item_queue: asyncio.Queue[object | None] = asyncio.Queue(maxsize=self.settings.collector_item_queue_size)
            candidate_pool: list[Listing] = []
            seen_item_ids: set[str] = set()
            consumer_error: list[Exception] = []
            queue_low_water = asyncio.Event()
            queue_low_water.set()
            candidate_condition = asyncio.Condition()
            assessment_finished = False

            async def rank_and_send(ranked_candidates: list[Listing]) -> None:
                if not ranked_candidates:
                    return
                self._raise_if_stopped(rule_id)
                session.commit()
                ranked_items = [self._llm_candidate(candidate) for candidate in ranked_candidates]
                ranking = await asyncio.to_thread(self.llm.rank, requirement, ranked_items)
                selected = {item.xianyu_item_id: item for item in ranking.items if item.recommended}
                if selected:
                    # 推送计数表示最终 candidate_ranking 选中的商品数量；先提交，
                    # 让前端在邮件传输结束前也能反映已产生的推荐结果。
                    task.sent_count += len(selected)
                    session.commit()
                    body = ["闲鱼商品筛选结果", ""]
                    for candidate in ranked_candidates:
                        result = selected.get(candidate.xianyu_item_id)
                        if result:
                            body.extend([f"价格：{candidate.price}", f"标题：{candidate.title}", f"链接：{candidate.url}",
                                         f"推荐理由：{result.reason}", f"风险：{'；'.join(result.risks) or '未发现明确风险'}", ""])
                    recipients = [recipient.email for recipient in rule.recipients]
                    await asyncio.to_thread(self.mailer.send, recipients, f"闲鱼筛选结果：{rule.product}", "\n".join(body))
                    for candidate in ranked_candidates:
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
                session.commit()

            async def assess_raw_items(raw_items: list[object]) -> None:
                self._raise_if_stopped(rule_id)
                item_dicts = [{"xianyu_item_id": raw.xianyu_item_id, "title": raw.title, "price": raw.price,
                               "url": raw.url, "image_url": raw.image_url, "seller_location": raw.seller_location,
                               "matched_search_queries": raw.matched_search_queries, "raw_data": raw.raw_data,
                               "collected_at": to_beijing_naive(datetime.fromisoformat(raw.collected_at.replace("Z", "+00:00")))}
                              for raw in raw_items]
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
                    # 初筛直接读取商品卡片，不再单独调用 ProductConditions 解析模型。
                    assessment = await asyncio.to_thread(self.llm.assess, requirement, self._llm_candidate(candidate))
                    return candidate if assessment.worthwhile else None

                for completed in asyncio.as_completed([assess_candidate(candidate) for candidate in new_candidates]):
                    candidate = await completed
                    if candidate is None:
                        continue
                    async with candidate_condition:
                        candidate_pool.append(candidate)
                        task.candidate_count += 1
                        # 单商品通过 item_assessment 后立刻持久化，供运行中的记录轮询显示。
                        session.commit()
                        candidate_condition.notify_all()
                session.commit()

            async def rank_candidates() -> None:
                """运行中只处理完整 20 条批次；收尾时再处理不足 20 条的剩余候选。"""
                try:
                    while True:
                        async with candidate_condition:
                            await candidate_condition.wait_for(
                                lambda: ((len(candidate_pool) >= self.RANKING_BATCH_SIZE
                                          and len(candidate_pool) > self.settings.candidate_pool_size
                                          * self.COLLECTION_RESUME_THRESHOLD)
                                         )
                                or assessment_finished
                                or bool(consumer_error),
                            )
                            if consumer_error:
                                raise consumer_error[0]
                            if not candidate_pool and assessment_finished:
                                return
                            count = (min(self.RANKING_BATCH_SIZE, len(candidate_pool))
                                     if assessment_finished else self.RANKING_BATCH_SIZE)
                            ranked_candidates = random.sample(candidate_pool, count)
                            ranked_ids = {candidate.id for candidate in ranked_candidates}
                            candidate_pool[:] = [candidate for candidate in candidate_pool
                                                 if candidate.id not in ranked_ids]
                            candidate_condition.notify_all()
                        await rank_and_send(ranked_candidates)
                except Exception as exc:
                    consumer_error.append(exc)
                    async with candidate_condition:
                        candidate_condition.notify_all()

            async def consume_items() -> None:
                while True:
                    # 高水位只运行排序；中水位初筛与排序 worker 可同时请求模型。
                    async with candidate_condition:
                        await candidate_condition.wait_for(
                            lambda: not self.should_start_ranking(
                                len(candidate_pool), self.settings.candidate_pool_size,
                            ) or bool(consumer_error),
                        )
                        if consumer_error:
                            raise consumer_error[0]
                    raw_item = await item_queue.get()
                    consumed_items = 1
                    stop_after_batch = False
                    try:
                        if raw_item is None:
                            return
                        if consumer_error:
                            continue
                        try:
                            batch_size = self.assessment_batch_size(
                                item_queue.qsize(), item_queue.maxsize, self.settings.initial_assessment_concurrency,
                            )
                            raw_items = [raw_item]
                            while len(raw_items) < batch_size:
                                try:
                                    next_item = item_queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    break
                                consumed_items += 1
                                if next_item is None:
                                    # 当前批次结束后退出；结束标记已从队列取走，不能再重复 task_done。
                                    stop_after_batch = True
                                    break
                                raw_items.append(next_item)
                            await assess_raw_items(raw_items)
                            if stop_after_batch:
                                return
                        except Exception as exc:  # 继续消费队列，避免生产端在满队列上永久等待。
                            consumer_error.append(exc)
                    finally:
                        for _ in range(consumed_items):
                            item_queue.task_done()
                        if self.is_collection_queue_low(item_queue.qsize(), item_queue.maxsize):
                            queue_low_water.set()

            async def on_page(raw_page: list) -> None:
                self._raise_if_stopped(rule_id)
                if consumer_error:
                    raise consumer_error[0]
                # 采集计数只反映浏览器实际解析到的原始商品卡片，不受后续过滤影响。
                task.scraped_count += len(raw_page)
                session.commit()
                for raw_item in raw_page:
                    await item_queue.put(raw_item)
                    if not self.is_collection_queue_low(item_queue.qsize(), item_queue.maxsize):
                        queue_low_water.clear()
                # 回调不返回，浏览器就不会点击下一页；借此实现高低水位的翻页节流。
                if self.should_pause_collection(item_queue.qsize(), item_queue.maxsize):
                    while not self.is_collection_queue_low(item_queue.qsize(), item_queue.maxsize):
                        await queue_low_water.wait()
                        if consumer_error:
                            raise consumer_error[0]

            consumer = asyncio.create_task(consume_items())
            ranking_worker = asyncio.create_task(rank_candidates())
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
                        await self.collector.search_pages(requirement.search_query, account.state_path, on_page, rule.max_pages)
                    else:
                        # 兼容外部实现仍只提供 search() 的采集器；生产 CollectorService 走逐页接口。
                        await on_page(await self.collector.search(requirement.search_query, account.state_path, rule.max_pages))
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
            await item_queue.put(None)
            await consumer
            assessment_finished = True
            async with candidate_condition:
                candidate_condition.notify_all()
            await ranking_worker
            if consumer_error:
                raise consumer_error[0]
            if not collected:
                raise RuntimeError("没有可用的闲鱼登录状态；请完成登录，或等待受限账号冷却后再试。")
            self._raise_if_stopped(rule_id)
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
            ranking_worker = locals().get("ranking_worker")
            if ranking_worker and not ranking_worker.done():
                ranking_worker.cancel()
                try:
                    await ranking_worker
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
