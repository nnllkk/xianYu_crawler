from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[2]


class LLMTaskConfig(BaseModel):
    provider: str = "ollama"
    model: str = "gemma4:latest"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ROOT_DIR / ".env"), extra="ignore")

    app_name: str = "xianyu-filter"
    database_url: str = "mysql+pymysql://root:password@127.0.0.1:3306/xianyu_filter"
    siliconflow_api_key: str = Field(default="", validation_alias="SILICONFLOW_API_KEY")
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    llm_provider: str = "ollama"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "gemma4:latest"
    ollama_context_length: int = 16384
    llm_model: str = "gemma4:latest"
    llm_timeout_seconds: int = 60
    llm_retry_count: int = 2
    scheduler_interval_minutes: int = 1
    schedule_min_interval_minutes: int = 10
    schedule_max_interval_minutes: int = 30
    search_delay_min_seconds: int = 5
    search_delay_max_seconds: int = 15
    max_task_retries: int = 3
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    timezone: str = "Asia/Shanghai"
    playwright_executable_path: str | None = Field(default=None, validation_alias="PLAYWRIGHT_EXECUTABLE_PATH")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    config_path = ROOT_DIR / "config.yaml"
    if config_path.exists():
        # YAML 只覆盖非敏感配置；密钥始终由环境变量提供。
        values = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        llm = values.get("llm", {})
        providers = llm.get("providers", {})
        siliconflow = providers.get("siliconflow", {})
        settings = settings.model_copy(update={
            "siliconflow_base_url": siliconflow.get("base_url", settings.siliconflow_base_url),
            "llm_provider": llm.get("default_provider", settings.llm_provider),
            "llm_model": llm.get("tasks", {}).get("llm1_user_parse", {}).get("model", settings.llm_model),
            "llm_retry_count": llm.get("retry_count", settings.llm_retry_count),
            "llm_timeout_seconds": llm.get("timeout_seconds", settings.llm_timeout_seconds),
            "ollama_base_url": values.get("ollama", {}).get("base_url", settings.ollama_base_url),
            "ollama_model": values.get("ollama", {}).get("model", settings.ollama_model),
            "ollama_context_length": values.get("ollama", {}).get("context_length", settings.ollama_context_length),
            "scheduler_interval_minutes": values.get("scheduler", {}).get("poll_interval_minutes", settings.scheduler_interval_minutes),
            "schedule_min_interval_minutes": values.get("scheduler", {}).get("min_interval_minutes", settings.schedule_min_interval_minutes),
            "schedule_max_interval_minutes": values.get("scheduler", {}).get("max_interval_minutes", settings.schedule_max_interval_minutes),
            "search_delay_min_seconds": values.get("collector", {}).get("search_delay_min_seconds", settings.search_delay_min_seconds),
            "search_delay_max_seconds": values.get("collector", {}).get("search_delay_max_seconds", settings.search_delay_max_seconds),
        })
    return settings
