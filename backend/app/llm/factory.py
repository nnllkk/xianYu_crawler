from collections.abc import Callable
import os

from ..config import Settings
from .provider import LLMProvider
from .mock import MockProvider
from .ollama import OllamaProvider
from .siliconflow import SiliconFlowProvider


ProviderBuilder = Callable[[Settings], LLMProvider]


class ProviderFactory:
    _builders: dict[str, ProviderBuilder] = {
        "ollama": OllamaProvider,
        "siliconflow": SiliconFlowProvider,
        "mock": MockProvider,
    }

    @classmethod
    def register(cls, name: str, builder: ProviderBuilder) -> None:
        cls._builders[name] = builder

    @classmethod
    def create(cls, settings: Settings, provider_name: str = "ollama") -> LLMProvider:
        provider_config = settings.llm_provider_configs.get(provider_name, {})
        provider_type = provider_config.get("type", provider_name)
        builder = cls._builders.get(provider_type)
        if builder is None:
            raise ValueError(f"未配置 LLM Provider: {provider_name}（类型：{provider_type}）")
        provider_settings = settings.model_copy(update={
            "ollama_base_url": provider_config.get("base_url", settings.ollama_base_url),
            "ollama_model": provider_config.get("model", settings.llm_default_model),
            "siliconflow_base_url": provider_config.get("base_url", settings.siliconflow_base_url),
            "llm_model": provider_config.get("model", settings.llm_default_model),
            "siliconflow_api_key": os.getenv(
                provider_config.get("api_key_env", "SILICONFLOW_API_KEY"), settings.siliconflow_api_key,
            ),
        })
        return builder(provider_settings)

    @classmethod
    def create_task_providers(cls, settings: Settings) -> dict[str, LLMProvider]:
        """按任务配置创建 provider；同一 provider 只实例化一次。"""
        task_names = {"user_requirement", "item_assessment", "candidate_ranking"}
        names = {settings.llm_task_provider_map.get(task, settings.llm_provider) for task in task_names}
        return {name: cls.create(settings, name) for name in names}
