from abc import ABC, abstractmethod


class LLMProvider(ABC):
    @abstractmethod
    def complete_json(self, *, system: str, user: str, schema_name: str) -> str:
        """返回模型的 JSON 文本；校验和业务解释由服务层负责。"""
