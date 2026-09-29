from openai import OpenAI

from ..config import Settings
from .provider import LLMProvider


class SiliconFlowProvider(LLMProvider):
    """SiliconFlow 使用 OpenAI 兼容 Chat Completions 协议。"""

    def __init__(self, settings: Settings) -> None:
        if not settings.siliconflow_api_key:
            raise RuntimeError("缺少 SILICONFLOW_API_KEY，无法调用 LLM")
        self.client = OpenAI(
            api_key=settings.siliconflow_api_key,
            base_url=settings.siliconflow_base_url,
            timeout=settings.llm_timeout_seconds,
        )
        self.model = settings.llm_model

    def complete_json(self, *, system: str, user: str, schema_name: str) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError(f"{schema_name} 返回空内容")
        return content
