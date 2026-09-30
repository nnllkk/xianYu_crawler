import json
import logging
import time
from typing import TypeVar

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
from pydantic import BaseModel, ValidationError

from ..config import Settings
from .provider import LLMProvider
from .schemas import ItemAssessment, PriceRange, RankedItem, RankingResult, UserRequirement


T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)


USER_REQUIREMENT_SYSTEM_PROMPT = """你是闲鱼二手商品需求解析器。
任务：把用户输入转换为 UserRequirement JSON，不输出解释、Markdown 或 Schema 以外字段。

必须遵守：
1. keyword 必须等于 product，不得换成泛化词或遗漏品牌/型号。
2. search_query 必须是一条可直接输入闲鱼搜索框的完整搜索语句；保留关键型号和用户明确规格，不得编造用户未提及的年份、成色或配件。
3. budget 为空时 price_range.min 和 max 都为 null；"6000-8000" 必须输出 min=6000、max=8000；"6000-" 只输出 min=6000；"-8000" 只输出 max=8000。不得因为搜索语句或商品推测改写预算。
4. conditions 必须覆盖 extra_conditions 的全部要求，使用独立自然语言字符串。缩写要标准化："32G+512G" 在笔记本语境表示"内存至少32GB"和"存储至少512GB"；"16寸"表示"屏幕尺寸16寸"。对于“不要维修机”等否定要求，也要保留否定含义。但当语境不能确定含义时保留原文，不要猜测。
5. original_input 必须逐字段原样保留 product、extra_conditions、budget。

示例：product="MacBook M1 Pro"，extra_conditions="16寸 32G+512G，不要维修机"，budget="6000-8000" 时，conditions 至少包含"屏幕尺寸16寸"、"内存至少32GB"、"存储至少512GB"和“不接受维修机”，price_range 为 {"min":6000,"max":8000}。"""


ITEM_ASSESSMENT_SYSTEM_PROMPT = """你是闲鱼二手商品关注资格判断器。
任务：根据 user_conditions、单个商品的 title/card_text 证据，直接判断该商品是否值得进入后续候选池。只输出 ItemAssessment JSON，不输出解释、Markdown 或 Schema 以外字段。

必须遵守：
1. xianyu_item_id 必须原样复制输入，不能生成、修改或省略。
2. 逐项比对用户条件。出现明确规格冲突、用户明确排除的情况或高风险时 worthwhile=false；将证据写入 risks。
3. 信息不足但没有明确冲突时 worthwhile=true 且 uncertain=true，使其进入候选池等待与其他商品比较；不得因缺少未提供的信息直接排除。
4. 价格已由后端完成预算筛选，不能因为价格决定 worthwhile。
5. reason 只用一句中文说明判断依据；risks 仅列输入中存在的风险、缺失信息或需买家确认点，不得编造事实。"""


RANKING_SYSTEM_PROMPT = """你是闲鱼二手商品候选排序器。
任务：仅比较已通过关注资格判断的候选商品，根据 user_conditions、商品 title/card_text 证据输出 RankingResult JSON；不输出解释、Markdown 或 Schema 以外字段。

必须遵守：
1. 只能返回输入 candidates 中存在的 xianyu_item_id；每个 ID 至多出现一次。
2. 按匹配程度、明确风险、信息完整度和价格综合排序；价格已由后端完成预算筛选，但可在同等匹配时作为排序因素。不得因卖家所在地单独决定。
3. recommended=true 表示应通知用户。对于信息不足但无明确冲突的商品，允许推荐，但必须 uncertain=true 并在 risks 标出需确认内容。
4. reason 用一句中文说明最重要的排序或推荐依据；risks 列出明确风险、缺失信息或需买家确认的点。不得编造未提供的事实。
5. score 为 0 到 100；rank 从 1 开始且不可重复。
6. 最多返回 5 个 recommended=true 的商品；不推荐商品可不返回。"""


class LLMService:
    def __init__(self, provider: LLMProvider, settings: Settings) -> None:
        self.provider = provider
        self.settings = settings

    def _request(self, schema: type[T], system: str, user: str) -> T:
        last_error: Exception | None = None
        for attempt in range(self.settings.llm_retry_count + 1):
            started = time.monotonic()
            try:
                content = self.provider.complete_json(system=system, user=user, schema_name=schema.__name__)
                result = schema.model_validate_json(content)
                logger.info("llm call succeeded", extra={"schema": schema.__name__, "attempt": attempt + 1,
                                                           "duration_ms": round((time.monotonic() - started) * 1000)})
                return result
            except APIStatusError as exc:
                last_error = exc
                logger.warning("llm call failed", extra={"schema": schema.__name__, "attempt": attempt + 1,
                                                         "error_type": type(exc).__name__, "status_code": exc.status_code,
                                                         "duration_ms": round((time.monotonic() - started) * 1000)})
                # 鉴权、余额和请求参数错误重试无意义；仅限流和服务端错误进入重试。
                if exc.status_code not in (429, 500, 502, 503, 504):
                    break
                if attempt < self.settings.llm_retry_count:
                    self._wait_before_retry(attempt, schema.__name__)
            except (APIConnectionError, APITimeoutError, RateLimitError, httpx.TimeoutException,
                    httpx.NetworkError, ValidationError, ValueError) as exc:
                last_error = exc
                logger.warning("llm call failed", extra={"schema": schema.__name__, "attempt": attempt + 1,
                                                         "error_type": type(exc).__name__,
                                                         "duration_ms": round((time.monotonic() - started) * 1000)})
                if attempt < self.settings.llm_retry_count:
                    self._wait_before_retry(attempt, schema.__name__)
            except Exception as exc:
                # 未知异常不重复调用，避免把编程错误伪装成瞬时故障。
                last_error = exc
                logger.exception("unexpected llm error", extra={"schema": schema.__name__, "attempt": attempt + 1})
                break
        raise RuntimeError(f"LLM {schema.__name__} 调用失败：{last_error}") from last_error

    def _wait_before_retry(self, attempt: int, schema_name: str) -> None:
        """线性退避：第 1 次重试等待 5 秒，之后每次增加 5 秒。"""
        delay = self.settings.llm_retry_delay_seconds * (attempt + 1)
        logger.info("waiting before llm retry", extra={"schema": schema_name, "delay_seconds": delay})
        time.sleep(delay)

    def parse_requirement(self, product: str, extra_conditions: str | None, budget: str | None) -> UserRequirement:
        payload = {"product": product, "extra_conditions": extra_conditions, "budget": budget}
        result = self._request(
            UserRequirement,
            USER_REQUIREMENT_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
        )
        # 预算是固定格式，不能让模型偶发遗漏破坏后续确定性筛选。
        result.price_range = self._parse_budget(budget)
        result.original_input.product = product
        result.original_input.extra_conditions = extra_conditions
        result.original_input.budget = budget
        return result

    @staticmethod
    def _parse_budget(budget: str | None) -> PriceRange:
        if not budget:
            return PriceRange()
        import re

        matched = re.fullmatch(r"\s*(\d+(?:\.\d+)?)?\s*-\s*(\d+(?:\.\d+)?)?\s*", budget)
        if not matched:
            raise ValueError("预算必须是 min-max、min- 或 -max 格式")
        return PriceRange(min=float(matched.group(1)) if matched.group(1) else None,
                          max=float(matched.group(2)) if matched.group(2) else None)

    def assess(self, requirement: UserRequirement, candidate: dict[str, object]) -> ItemAssessment:
        payload = {"user_conditions": requirement.conditions, "candidate": candidate}
        result = self._request(ItemAssessment, ITEM_ASSESSMENT_SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False))
        if result.xianyu_item_id != str(candidate["xianyu_item_id"]):
            raise RuntimeError("LLM ItemAssessment 返回了不属于当前商品的 ID")
        return result

    def rank(self, requirement: UserRequirement, candidates: list[dict[str, object]]) -> RankingResult:
        payload = {"user_conditions": requirement.conditions, "candidates": candidates}
        result = self._request(
            RankingResult,
            RANKING_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
        )
        candidate_ids = {str(candidate["xianyu_item_id"]) for candidate in candidates}
        valid_items = [item for item in result.items if item.xianyu_item_id in candidate_ids]
        return RankingResult(items=sorted(valid_items, key=lambda item: (item.rank, -item.score))[:5])
