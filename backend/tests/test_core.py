import sys
import asyncio
import json
import httpx
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.llm.schemas import UserRequirement  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.llm.factory import ProviderFactory  # noqa: E402
from app.llm.ollama import OllamaProvider  # noqa: E402
from app.llm.service import LLMService  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import NotificationState, TaskRun, WatchRule, RuleRecipient, XianyuAccount, beijing_now  # noqa: E402
from app.services.task_runner import TaskRunner, TaskStoppedError  # noqa: E402
from app.services.xianyu_accounts import XianyuAccountService  # noqa: E402
from app.api import sync_rule_recipients  # noqa: E402
from app.schemas import RuleCreate  # noqa: E402
import app.services.task_runner as task_runner_module  # noqa: E402
import app.services.collector as collector_module  # noqa: E402
import search_xianyu as search_xianyu_module  # noqa: E402
from app.services.filtering import parse_budget  # noqa: E402
from app.services.collector import (  # noqa: E402
    AccessLimitedError,
    CollectionError,
    CollectorService,
    LoginRequiredError,
)


def test_parse_budget_range() -> None:
    assert parse_budget("6000-8000") == (6000, 8000)
    assert parse_budget("6000-") == (6000, None)
    assert parse_budget("-8000") == (None, 8000)


def test_rule_page_limit_defaults_to_twenty_and_accepts_user_choice() -> None:
    default_rule = RuleCreate(product="MacBook", emails=["to@test.com"])
    selected_rule = RuleCreate(product="MacBook", emails=["to@test.com"], max_pages=7)
    assert default_rule.max_pages == 20
    assert selected_rule.max_pages == 7


def test_collector_uses_headed_browser_by_default(monkeypatch) -> None:
    observed = {}

    async def fake_run_query(query, headless, max_pages, *_args):
        observed.update(query=query, headless=headless, max_pages=max_pages)
        return []

    monkeypatch.setattr(collector_module, "run_query", fake_run_query)
    assert asyncio.run(CollectorService(Settings()).search("MacBook", "/tmp/state.json", 6)) == []
    assert observed == {"query": "MacBook", "headless": False, "max_pages": 6}


def test_pipeline_limits_are_configurable() -> None:
    settings = Settings(
        collector_item_queue_size=60,
        candidate_pool_size=100,
        initial_assessment_concurrency=4,
        llm_item_assessment_context_length=3072,
        llm_candidate_ranking_context_length=8192,
    )
    assert settings.collector_item_queue_size == 60
    assert settings.candidate_pool_size == 100
    assert settings.initial_assessment_concurrency == 4
    assert settings.llm_item_assessment_context_length == 3072
    assert settings.llm_candidate_ranking_context_length == 8192


def test_llm_default_model_is_loaded_from_config() -> None:
    settings = get_settings()
    assert settings.llm_default_model == "gemma4:latest"
    assert "ollama" in settings.llm_provider_configs
    assert "model" not in settings.llm_provider_configs["ollama"]


def test_assessment_parallelism_only_starts_above_queue_high_water_mark() -> None:
    assert TaskRunner.assessment_batch_size(1, 2, 2) == 1
    assert TaskRunner.assessment_batch_size(2, 2, 2) == 2
    assert TaskRunner.assessment_batch_size(3, 4, 2) == 1
    assert TaskRunner.assessment_batch_size(4, 4, 2) == 2


def test_collection_and_ranking_water_marks_are_hysteretic() -> None:
    assert TaskRunner.should_pause_collection(48, 60)
    assert not TaskRunner.should_pause_collection(47, 60)
    assert TaskRunner.is_collection_queue_low(12, 60)
    assert not TaskRunner.is_collection_queue_low(13, 60)
    assert TaskRunner.should_start_ranking(71, 100)
    assert not TaskRunner.should_start_ranking(70, 100)
    assert TaskRunner.is_candidate_pool_low(20, 100)
    assert not TaskRunner.is_candidate_pool_low(21, 100)


def test_ollama_uses_context_length_for_each_task(monkeypatch) -> None:
    requested_contexts = []

    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"message": {"content": "{}"}}

    def fake_post(_url, **kwargs):
        requested_contexts.append(kwargs["json"]["options"]["num_ctx"])
        return Response()

    import app.llm.ollama as ollama_module

    monkeypatch.setattr(ollama_module.httpx, "post", fake_post)
    provider = OllamaProvider(Settings(
        llm_user_requirement_context_length=1024,
        llm_item_assessment_context_length=3072,
        llm_candidate_ranking_context_length=4096,
    ))
    for schema_name in ("UserRequirement", "ItemAssessment", "RankingResult"):
        provider.complete_json(system="test", user="{}", schema_name=schema_name)
    assert requested_contexts == [1024, 3072, 4096]


def test_collector_prefers_chrome_channel() -> None:
    calls = []

    class Chromium:
        async def launch(self, **kwargs):
            calls.append(kwargs)
            return object()

    browser = asyncio.run(
        search_xianyu_module.launch_collector_browser(
            SimpleNamespace(chromium=Chromium()),
            headless=False,
            executable_path=None,
        )
    )
    assert browser is not None
    assert calls == [{"headless": False, "channel": "chrome"}]


def test_collector_falls_back_when_chrome_channel_is_unavailable() -> None:
    calls = []

    class Chromium:
        async def launch(self, **kwargs):
            calls.append(kwargs)
            if kwargs.get("channel") == "chrome":
                raise RuntimeError("Chrome not found")
            return object()

    browser = asyncio.run(
        search_xianyu_module.launch_collector_browser(
            SimpleNamespace(chromium=Chromium()),
            headless=True,
            executable_path=None,
        )
    )
    assert browser is not None
    assert calls == [{"headless": True, "channel": "chrome"}, {"headless": True}]


def test_requirement_requires_one_search_query() -> None:
    requirement = UserRequirement.model_validate({
        "keyword": "MacBook M1 Pro",
        "search_query": "MacBook M1 Pro 16寸 32G 512G",
        "price_range": {"min": 6000, "max": 8000},
        "conditions": ["内存至少32GB"],
        "original_input": {"product": "MacBook M1 Pro", "extra_conditions": "32G", "budget": "6000-8000"},
    })
    assert requirement.search_query == "MacBook M1 Pro 16寸 32G 512G"


def test_mock_llm_contracts() -> None:
    settings = Settings(llm_provider="mock", llm_retry_count=0)
    service = LLMService(ProviderFactory.create(settings, "mock"), settings)
    requirement = service.parse_requirement("MacBook M1 Pro", "16寸+32G", "6000-8000")
    assert requirement.search_query == "MacBook M1 Pro"
    assessment = service.assess(requirement, {"xianyu_item_id": "1", "title": "MacBook", "price": 6000,
                                              "raw_data": {"card_text": "16寸"}})
    assert assessment.worthwhile
    result = service.rank(requirement, [{"xianyu_item_id": "1", "title": "MacBook", "price": 6000,
                                         "raw_data": {"card_text": "16寸"}}])
    assert result.items[0].xianyu_item_id == "1"


def test_llm_retries_with_linear_backoff(monkeypatch) -> None:
    attempts = []
    delays = []

    class RetryingProvider:
        def complete_json(self, **kwargs):
            attempts.append(kwargs["schema_name"])
            if len(attempts) <= 3:
                raise httpx.ReadTimeout("模型暂时无响应")
            return '{"keyword":"MacBook","search_query":"MacBook","price_range":{"min":null,"max":null},"conditions":[],"original_input":{"product":"MacBook"}}'

    monkeypatch.setattr("app.llm.service.time.sleep", delays.append)
    settings = Settings(llm_retry_count=3, llm_retry_delay_seconds=5)
    service = LLMService(RetryingProvider(), settings)
    result = service.parse_requirement("MacBook", None, None)

    assert result.keyword == "MacBook"
    assert attempts == ["UserRequirement"] * 4
    assert delays == [5, 10, 15]


def test_llm_routes_tasks_to_configured_providers() -> None:
    class RecordingProvider:
        def __init__(self, name: str) -> None:
            self.name = name
            self.calls = []

        def complete_json(self, *, system, user, schema_name):
            self.calls.append(schema_name)
            if schema_name == "UserRequirement":
                return '{"keyword":"MacBook","search_query":"MacBook","price_range":{"min":null,"max":null},"conditions":[],"original_input":{"product":"MacBook"}}'
            if schema_name == "ItemAssessment":
                return '{"xianyu_item_id":"1","worthwhile":true,"reason":"ok","risks":[],"uncertain":false}'
            return '{"items":[]}'

    settings = Settings(
        llm_provider="ollama",
        llm_task_provider_map={"user_requirement": "remote", "item_assessment": "local", "candidate_ranking": "remote"},
    )
    local = RecordingProvider("local")
    remote = RecordingProvider("remote")
    service = LLMService(local, settings, {"local": local, "remote": remote, "ollama": local})
    requirement = service.parse_requirement("MacBook", None, None)
    service.assess(requirement, {"xianyu_item_id": "1", "title": "MacBook", "price": 1})
    service.rank(requirement, [])

    assert remote.calls == ["UserRequirement", "RankingResult"]
    assert local.calls == ["ItemAssessment"]


def test_llm_logs_request_and_response_for_each_task(caplog) -> None:
    settings = Settings(llm_provider="mock", llm_retry_count=0)
    service = LLMService(ProviderFactory.create(settings, "mock"), settings)
    caplog.set_level(logging.INFO, logger="app.llm.service")

    requirement = service.parse_requirement("MacBook", "16寸", "6000-8000")
    service.assess(requirement, {"xianyu_item_id": "1", "title": "MacBook 16寸", "price": 7000})
    service.rank(requirement, [{"xianyu_item_id": "1", "title": "MacBook 16寸", "price": 7000}])

    messages = [record.getMessage() for record in caplog.records]
    for task_name in ("user_requirement", "item_assessment", "candidate_ranking"):
        assert any(message.startswith(f"llm request sent task={task_name}") for message in messages)
        assert any(message.startswith(f"llm response received task={task_name}") for message in messages)


def test_requirement_budget_is_deterministic_when_model_omits_it() -> None:
    class BudgetOmittingProvider:
        def complete_json(self, **_kwargs):
            return '{"keyword":"MacBook","search_query":"MacBook 16寸","price_range":{"min":null,"max":null},"conditions":[],"original_input":{"product":"wrong"}}'

    service = LLMService(BudgetOmittingProvider(), Settings(llm_retry_count=0))
    result = service.parse_requirement("MacBook", "16寸，不要维修机", "6000-8000")
    assert result.price_range.min == 6000
    assert result.price_range.max == 8000
    assert result.original_input.extra_conditions == "16寸，不要维修机"
    assert result.original_input.product == "MacBook"


def _add_active_account(session: Session, state_path: Path) -> None:
    state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    session.add(XianyuAccount(name="test-account", state_path=str(state_path), status="active"))


def test_expired_account_cooldown_becomes_active(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    state_path = tmp_path / "cooled-account.json"
    state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    service = XianyuAccountService(Settings())
    with Session(engine) as session:
        account = XianyuAccount(
            name="cooled-account",
            state_path=str(state_path),
            status="cooldown",
            cooldown_until=beijing_now() - timedelta(seconds=1),
        )
        session.add(account)
        session.commit()

        assert service.release_expired_cooldowns(session)
        session.commit()
        assert account.status == "active"
        assert account.cooldown_until is None
        assert service.choose_available(session, set()) == account


def test_rule_recipient_update_preserves_unchanged_email(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        rule = WatchRule(product="MacBook", budget="1-2")
        rule.recipients = [RuleRecipient(email="same@example.com")]
        session.add(rule)
        session.commit()
        preserved_id = rule.recipients[0].id

        sync_rule_recipients(rule, ["same@example.com", "new@example.com"])
        session.commit()
        recipients = {recipient.email: recipient.id for recipient in rule.recipients}
        assert set(recipients) == {"same@example.com", "new@example.com"}
        assert recipients["same@example.com"] == preserved_id


def _run_account_failure_case(monkeypatch, tmp_path: Path, error: Exception) -> tuple[XianyuAccount, TaskRun]:
    """运行一条采集失败任务，供账号状态分类测试复用。"""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(task_runner_module, "SessionLocal", session_factory)
    settings = Settings(llm_provider="mock", llm_retry_count=0)
    service = LLMService(ProviderFactory.create(settings, "mock"), settings)
    with Session(engine) as session:
        rule = WatchRule(product="MacBook", budget="1-2")
        session.add(rule)
        _add_active_account(session, tmp_path / "test-account.json")
        session.commit()
        rule_id = rule.id

    runner = TaskRunner(settings, service)

    async def fail_search(_query, _state_path, _max_pages):
        raise error

    runner.collector = SimpleNamespace(search=fail_search)
    try:
        asyncio.run(runner.run(rule_id))
    except RuntimeError:
        pass

    with Session(engine) as session:
        account = session.scalar(select(XianyuAccount))
        task = session.scalar(select(TaskRun))
        session.expunge(account)
        session.expunge(task)
        return account, task


def test_login_expiry_marks_account_needs_login_without_cooldown(monkeypatch, tmp_path: Path) -> None:
    account, task = _run_account_failure_case(
        monkeypatch,
        tmp_path,
        LoginRequiredError("闲鱼登录态已失效，已跳转到登录页面"),
    )
    assert account.status == "needs_login"
    assert account.cooldown_until is None
    assert "没有可用的闲鱼登录状态" in task.error_message


def test_access_limit_marks_account_cooldown(monkeypatch, tmp_path: Path) -> None:
    account, _task = _run_account_failure_case(
        monkeypatch,
        tmp_path,
        AccessLimitedError("闲鱼页面未允许自动化访问，检测到：安全验证。"),
    )
    assert account.status == "cooldown"
    assert account.cooldown_until is not None


def test_general_collection_error_keeps_account_active(monkeypatch, tmp_path: Path) -> None:
    account, task = _run_account_failure_case(
        monkeypatch,
        tmp_path,
        CollectionError("未找到闲鱼搜索框，页面结构可能已变化。"),
    )
    assert account.status == "active"
    assert account.cooldown_until is None
    assert "未找到闲鱼搜索框" in task.error_message


def test_unexpected_collection_error_keeps_account_active(monkeypatch, tmp_path: Path) -> None:
    account, task = _run_account_failure_case(
        monkeypatch,
        tmp_path,
        RuntimeError("浏览器启动失败"),
    )
    assert account.status == "active"
    assert account.cooldown_until is None
    assert "浏览器启动失败" in task.error_message


def test_collector_wraps_unexpected_error_as_general_collection_error(monkeypatch) -> None:
    async def fail_run_query(*_args, **_kwargs):
        raise RuntimeError("浏览器启动失败")

    monkeypatch.setattr(search_xianyu_module, "_run_query", fail_run_query)
    try:
        asyncio.run(search_xianyu_module.run_query("MacBook", False, 1))
    except CollectionError as exc:
        assert "浏览器启动失败" in str(exc)
    else:
        raise AssertionError("未知采集错误必须包装为 CollectionError")


def test_collect_waits_for_search_result_cards() -> None:
    class DelayedCards:
        first = None

        def __init__(self) -> None:
            self.first = self
            self.waited = False

        async def wait_for(self, **kwargs) -> None:
            self.waited = kwargs == {"state": "visible", "timeout": 10_000}

        async def count(self) -> int:
            return 30

    class Page:
        def __init__(self) -> None:
            self.cards = DelayedCards()

        def locator(self, selector: str) -> DelayedCards:
            assert selector == search_xianyu_module.CARD_SELECTORS[0]
            return self.cards

    page = Page()
    cards = asyncio.run(search_xianyu_module.get_cards(page))
    assert cards is page.cards
    assert page.cards.waited


def test_next_page_selector_targets_goofish_right_arrow() -> None:
    assert search_xianyu_module.NEXT_PAGE_SELECTORS[0] == (
        'button[class*="search-pagination-arrow-container"]:has('
        '[class*="search-pagination-arrow-right"])'
    )


def test_wait_for_next_page_results_waits_for_new_first_link() -> None:
    class Page:
        def __init__(self) -> None:
            self.expression = None
            self.kwargs = None

        async def wait_for_function(self, expression, **kwargs) -> None:
            self.expression = expression
            self.kwargs = kwargs

    page = Page()
    asyncio.run(
        search_xianyu_module.wait_for_next_page_results(page, "/item?id=old")
    )
    assert "firstCard.getAttribute('href') !== previousHref" in page.expression
    assert page.kwargs == {"arg": "/item?id=old", "timeout": 10_000}


def test_offline_task_pipeline(monkeypatch, tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(task_runner_module, "SessionLocal", session_factory)
    settings = Settings(llm_provider="mock", llm_retry_count=0, smtp_host="smtp.test", smtp_from="from@test")
    service = LLMService(ProviderFactory.create(settings, "mock"), settings)
    with Session(engine) as session:
        requirement = UserRequirement(
            keyword="MacBook",
            search_query="MacBook",
            price_range={"min": 1, "max": 10},
            conditions=["16寸", "32G"],
            original_input={"product": "MacBook", "extra_conditions": "16寸+32G", "budget": "1-10"},
        )
        rule = WatchRule(
            product="MacBook",
            extra_conditions="16寸+32G",
            budget="1-10",
            parsed_requirement=requirement.model_dump(mode="json"),
        )
        rule.recipients = [RuleRecipient(email="to@test")]
        session.add(rule)
        _add_active_account(session, tmp_path / "test-account.json")
        session.commit()
        rule_id = rule.id

    runner = TaskRunner(settings, service)
    monkeypatch.setattr(
        service,
        "parse_requirement",
        lambda *_args: (_ for _ in ()).throw(AssertionError("已缓存的需求不应重新解析")),
    )
    received_queries = []

    async def collect_one_query(query, _state_path, max_pages):
        received_queries.append(query)
        assert max_pages == 20
        return [SimpleNamespace(
            xianyu_item_id="item-1", title="MacBook 16寸 32G", price=5.0, url="https://example.test/item-1",
            image_url=None, seller_location="广东", raw_data={"card_text": "16寸 32G"},
            matched_search_queries=["MacBook"], collected_at=datetime.now(timezone.utc).isoformat(),
        )]

    runner.collector = SimpleNamespace(search=collect_one_query)
    sent = []
    runner.mailer = SimpleNamespace(send=lambda recipients, subject, body: sent.append((recipients, subject, body)))
    task_id = asyncio.run(runner.run(rule_id))

    with Session(engine) as session:
        task = session.get(TaskRun, task_id)
        state = session.scalar(select(NotificationState))
        assert task.status == "success"
        assert task.sent_count == 1
        assert task.search_queries == ["MacBook"]
        assert state.email == "to@test"
    assert received_queries == ["MacBook"]
    assert sent and "item-1" in sent[0][2]


def test_task_drains_remaining_candidates_in_final_ranking(monkeypatch, tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(task_runner_module, "SessionLocal", session_factory)
    settings = Settings(
        llm_provider="mock", llm_retry_count=0, smtp_host="smtp.test", smtp_from="from@test",
        candidate_pool_size=28, initial_assessment_concurrency=2,
    )
    service = LLMService(ProviderFactory.create(settings, "mock"), settings)
    with Session(engine) as session:
        rule = WatchRule(product="MacBook", budget="1-10", max_pages=3)
        rule.recipients = [RuleRecipient(email="to@test")]
        session.add(rule)
        _add_active_account(session, tmp_path / "test-account.json")
        session.commit()
        rule_id = rule.id

    def raw_item(item_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            xianyu_item_id=item_id, title=f"MacBook {item_id}", price=5.0,
            url=f"https://example.test/{item_id}", image_url=None, seller_location="广东",
            raw_data={"card_text": item_id}, matched_search_queries=["MacBook"],
            collected_at=datetime.now(timezone.utc).isoformat(),
        )

    async def collect_pages(_query, _state_path, on_page, max_pages):
        assert max_pages == 3
        await on_page([raw_item("item-1"), raw_item("item-2")])
        await on_page([raw_item("item-3")])
        return []

    runner = TaskRunner(settings, service)
    assert settings.candidate_pool_size == 28
    assert settings.initial_assessment_concurrency == 2
    runner.collector = SimpleNamespace(search_pages=collect_pages)
    runner.mailer = SimpleNamespace(send=lambda *_args: None)
    ranked_batches = []
    original_rank = service.rank
    def record_rank(requirement, candidates):
        ranked_batches.append([candidate["xianyu_item_id"] for candidate in candidates])
        return original_rank(requirement, candidates)

    monkeypatch.setattr(service, "rank", record_rank)
    task_id = asyncio.run(runner.run(rule_id))

    with Session(engine) as session:
        task = session.get(TaskRun, task_id)
        assert task.scraped_count == 3
        assert task.candidate_count == 3
        assert task.sent_count == 3
    assert len(ranked_batches) == 1
    assert set(ranked_batches[0]) == {"item-1", "item-2", "item-3"}


def test_assessment_and_ranking_run_concurrently_in_candidate_mid_range(monkeypatch, tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(task_runner_module, "SessionLocal", session_factory)
    settings = Settings(
        llm_provider="mock", llm_retry_count=0, smtp_host="smtp.test", smtp_from="from@test",
        candidate_pool_size=100, collector_item_queue_size=30, initial_assessment_concurrency=1,
    )
    service = LLMService(ProviderFactory.create(settings, "mock"), settings)
    with Session(engine) as session:
        rule = WatchRule(product="MacBook", budget="1-10", max_pages=1)
        rule.recipients = [RuleRecipient(email="to@test")]
        session.add(rule)
        _add_active_account(session, tmp_path / "test-account.json")
        session.commit()
        rule_id = rule.id

    def raw_item(index: int) -> SimpleNamespace:
        return SimpleNamespace(
            xianyu_item_id=f"item-{index}", title=f"MacBook {index}", price=5.0,
            url=f"https://example.test/{index}", image_url=None, seller_location="广东",
            raw_data={"card_text": str(index)}, matched_search_queries=["MacBook"],
            collected_at=datetime.now(timezone.utc).isoformat(),
        )

    ranking_started = threading.Event()
    state = {"sixth_assessment_overlapped": False}

    ranked_batch_sizes = []

    def assess(_requirement, candidate):
        if candidate["xianyu_item_id"] == "item-22":
            # to_thread 会释放事件循环；排序 worker 必须能在当前初筛未完成时开始。
            state["sixth_assessment_overlapped"] = ranking_started.wait(timeout=1)
        return SimpleNamespace(worthwhile=True)

    def rank(_requirement, candidates):
        ranking_started.set()
        ranked_batch_sizes.append(len(candidates))
        return SimpleNamespace(items=[])

    async def collect_pages(_query, _state_path, on_page, _max_pages):
        await on_page([raw_item(index) for index in range(1, 24)])
        return []

    runner = TaskRunner(settings, service)
    runner.collector = SimpleNamespace(search_pages=collect_pages)
    runner.mailer = SimpleNamespace(send=lambda *_args: None)
    monkeypatch.setattr(service, "assess", assess)
    monkeypatch.setattr(service, "rank", rank)

    asyncio.run(runner.run(rule_id))
    assert state["sixth_assessment_overlapped"]
    assert ranked_batch_sizes[0] == 20


def test_stop_marks_task_stopped(monkeypatch, tmp_path: Path) -> None:
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
        _add_active_account(session, tmp_path / "test-account.json")
        session.commit()
        rule_id = rule.id

    runner = TaskRunner(settings, service)

    async def stop_during_search(_query, _state_path, _max_pages):
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
