from collections.abc import Callable

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
        builder = cls._builders.get(provider_name)
        if builder is None:
            raise ValueError(f"未配置 LLM Provider: {provider_name}")
        return builder(settings)
