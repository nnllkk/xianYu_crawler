import sys
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.llm.schemas import UserRequirement  # noqa: E402
from app.config import Settings  # noqa: E402
from app.llm.factory import ProviderFactory  # noqa: E402
from app.llm.service import LLMService  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import NotificationState, TaskRun, WatchRule, RuleRecipient  # noqa: E402
from app.services.task_runner import TaskRunner, TaskStoppedError  # noqa: E402
import app.services.task_runner as task_runner_module  # noqa: E402
from app.services.filtering import parse_budget  # noqa: E402


def test_parse_budget_range() -> None:
    assert parse_budget("6000-8000") == (6000, 8000)
    assert parse_budget("6000-") == (6000, None)
    assert parse_budget("-8000") == (None, 8000)


def test_requirement_requires_three_search_queries() -> None:
    requirement = UserRequirement.model_validate({
        "keyword": "MacBook M1 Pro",
        "search_queries": ["a", "b", "c"],
        "price_range": {"min": 6000, "max": 8000},
        "conditions": ["内存至少32GB"],
        "exclude_keywords": [],
        "original_input": {"product": "MacBook M1 Pro", "extra_conditions": "32G", "budget": "6000-8000"},
    })
    assert len(requirement.search_queries) == 3


def test_mock_llm_contracts() -> None:
    settings = Settings(llm_provider="mock", llm_retry_count=0)
    service = LLMService(ProviderFactory.create(settings, "mock"), settings)
    requirement = service.parse_requirement("MacBook M1 Pro", "16寸+32G", "6000-8000", [])
    assert len(requirement.search_queries) == 3
    product = service.parse_product({"xianyu_item_id": "1", "title": "MacBook", "raw_data": {"card_text": "16寸"}})
    result = service.rank(requirement, [{"xianyu_item_id": "1", "price": 6000, "conditions": product.conditions}])
    assert result.items[0].xianyu_item_id == "1"


def test_requirement_budget_is_deterministic_when_model_omits_it() -> None:
    class BudgetOmittingProvider:
        def complete_json(self, **_kwargs):
            return '{"keyword":"MacBook","search_queries":["a","b","c"],"price_range":{"min":null,"max":null},"conditions":[],"exclude_keywords":[],"original_input":{"product":"wrong"}}'

    service = LLMService(BudgetOmittingProvider(), Settings(llm_retry_count=0))
    result = service.parse_requirement("MacBook", "16寸", "6000-8000", ["维修机"])
    assert result.price_range.min == 6000
    assert result.price_range.max == 8000
    assert result.exclude_keywords == ["维修机"]
    assert result.original_input.product == "MacBook"


def test_offline_task_pipeline(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(task_runner_module, "SessionLocal", session_factory)
    settings = Settings(llm_provider="mock", llm_retry_count=0, smtp_host="smtp.test", smtp_from="from@test")
    service = LLMService(ProviderFactory.create(settings, "mock"), settings)
    with Session(engine) as session:
        rule = WatchRule(product="MacBook", extra_conditions="16寸+32G", budget="1-10")
        rule.recipients = [RuleRecipient(email="to@test")]
        session.add(rule)
        session.commit()
        rule_id = rule.id

    runner = TaskRunner(settings, service)
    runner.collector = SimpleNamespace(search=lambda queries: asyncio.sleep(0, result=[SimpleNamespace(
        xianyu_item_id="item-1", title="MacBook 16寸 32G", price=5.0, url="https://example.test/item-1",
        image_url=None, seller_location="广东", raw_data={"card_text": "16寸 32G"},
        matched_search_queries=["MacBook"],
        collected_at=datetime.now(timezone.utc).isoformat())]))
    sent = []
    runner.mailer = SimpleNamespace(send=lambda recipients, subject, body: sent.append((recipients, subject, body)))
    task_id = asyncio.run(runner.run(rule_id))

    with Session(engine) as session:
        task = session.get(TaskRun, task_id)
        state = session.scalar(select(NotificationState))
        assert task.status == "success"
        assert task.sent_count == 1
        assert state.email == "to@test"
    assert sent and "item-1" in sent[0][2]


def test_stop_marks_task_stopped(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(task_runner_module, "SessionLocal", session_factory)
    settings = Settings(llm_provider="mock", llm_retry_count=0)
    service = LLMService(ProviderFactory.create(settings, "mock"), settings)
    with Session(engine) as session:
        rule = WatchRule(product="x", budget="1-2")
        rule.recipients = [RuleRecipient(email="to@test")]
        session.add(rule)
        session.commit()
        rule_id = rule.id

    runner = TaskRunner(settings, service)

    async def stop_during_search(_queries):
        TaskRunner.request_stop(rule_id)
        return []

    runner.collector = SimpleNamespace(search=stop_during_search)
    try:
        asyncio.run(runner.run(rule_id))
    except TaskStoppedError:
        pass
    with Session(engine) as session:
        task = session.scalar(select(TaskRun).where(TaskRun.rule_id == rule_id))
        assert task.status == "stopped"
