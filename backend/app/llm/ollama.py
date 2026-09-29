import httpx

from ..config import Settings
from .schemas import ProductConditions, RankingResult, UserRequirement
from .provider import LLMProvider


class OllamaProvider(LLMProvider):
    """Ollama 本地 Chat API 适配器，不需要 API Key。"""

    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.ollama_base_url.rstrip("/")
        self.model = settings.ollama_model
        self.timeout = settings.llm_timeout_seconds
        self.context_length = settings.ollama_context_length

    def complete_json(self, *, system: str, user: str, schema_name: str) -> str:
        schemas = {
            "UserRequirement": UserRequirement,
            "ProductConditions": ProductConditions,
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
                # LLM2 同时携带多个商品的标题和卡片证据，默认 4096 token
                # 容易截断输入并造成空响应；长度由本地配置控制。
                "options": {"temperature": 0, "num_ctx": self.context_length},
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        content = response.json().get("message", {}).get("content")
        if not content:
            raise RuntimeError(f"{schema_name} 返回空内容")
        return content
