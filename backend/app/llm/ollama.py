import httpx

from ..config import Settings
from .schemas import ItemAssessment, ProductConditions, RankingResult, UserRequirement
from .provider import LLMProvider


class OllamaProvider(LLMProvider):
    """Ollama 本地 Chat API 适配器，不需要 API Key。"""

    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.ollama_base_url.rstrip("/")
        self.model = settings.ollama_model
        self.timeout = settings.llm_timeout_seconds
        self.context_lengths = {
            "UserRequirement": settings.llm_user_requirement_context_length,
            "ProductConditions": settings.llm_product_parse_context_length,
            "ItemAssessment": settings.llm_item_assessment_context_length,
            "RankingResult": settings.llm_candidate_ranking_context_length,
        }

    def complete_json(self, *, system: str, user: str, schema_name: str) -> str:
        schemas = {
            "UserRequirement": UserRequirement,
            "ProductConditions": ProductConditions,
            "ItemAssessment": ItemAssessment,
            "RankingResult": RankingResult,
        }
        response = httpx.post(
            f"{self.base_url}/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "format": schemas[schema_name].model_json_schema() if schema_name in schemas else "json",
                # 各任务按输入规模使用独立上下文，避免高频卡片解析占用排序所需的 16K 上下文。
                "options": {"temperature": 0, "num_ctx": self.context_lengths[schema_name]},
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        content = response.json().get("message", {}).get("content")
        if not content:
            raise RuntimeError(f"{schema_name} 返回空内容")
        return content
