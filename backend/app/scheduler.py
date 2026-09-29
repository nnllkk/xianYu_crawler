import asyncio
import logging
import random
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import desc, select

from .config import get_settings
from .database import SessionLocal
from .llm.service import LLMService
from .llm.factory import ProviderFactory
from .models import TaskRun, WatchRule, beijing_now
from .services.task_runner import TaskRunner, TaskStoppedError

logger = logging.getLogger(__name__)


class RuleScheduler:
    def __init__(self) -> None:
        self.scheduler = BackgroundScheduler(timezone=get_settings().timezone)
        self.scheduler.add_job(self._dispatch_enabled_rules, "interval",
                               minutes=get_settings().scheduler_interval_minutes,
                               id="enabled-rules", replace_existing=True)

    def start(self) -> None:
        self.scheduler.start()

    def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)

    def _dispatch_enabled_rules(self) -> None:
        session = SessionLocal()
        try:
            rules = session.scalars(select(WatchRule).where(WatchRule.is_enabled.is_(True))).all()
            due_rule_ids = []
            now = beijing_now()
            for rule in rules:
                latest = session.scalar(select(TaskRun).where(TaskRun.rule_id == rule.id)
                                        .order_by(desc(TaskRun.created_at)).limit(1))
                if latest and latest.status in {"pending", "running"}:
                    continue
                if rule.next_run_at and rule.next_run_at > now:
                    continue
                rule.next_run_at = now + timedelta(minutes=random.randint(
                    get_settings().schedule_min_interval_minutes,
                    get_settings().schedule_max_interval_minutes,
                ))
                due_rule_ids.append(rule.id)
            session.commit()
        finally:
            session.close()
        for rule_id in due_rule_ids:
            try:
                asyncio.run(self._run_with_retry(rule_id))
            except Exception:
                logger.exception("scheduled rule failed", extra={"rule_id": rule_id})

    async def _run_with_retry(self, rule_id: str) -> None:
        settings = get_settings()
        runner = TaskRunner(settings, LLMService(ProviderFactory.create(settings, settings.llm_provider), settings))
        session = SessionLocal()
        task = TaskRun(rule_id=rule_id, status="pending", stage="pending")
        session.add(task)
        session.commit()
        task_id = task.id
        session.close()
        last_error = None
        for attempt in range(settings.max_task_retries):
            try:
                await runner.run(rule_id, task_id)
                return
            except TaskStoppedError:
                return
            except Exception as exc:
                last_error = exc
                logger.warning("rule retry", extra={"rule_id": rule_id, "attempt": attempt + 1})
        raise RuntimeError(f"任务重试 {settings.max_task_retries} 次仍失败: {last_error}") from last_error
