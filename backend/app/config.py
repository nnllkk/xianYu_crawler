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
    llm_model: str = "gemma4:latest"
    llm_timeout_seconds: int = 60
    llm_retry_count: int = 2
    llm_user_requirement_context_length: int = Field(default=4096, ge=1)
    llm_product_parse_context_length: int = Field(default=4096, ge=1)
    llm_item_assessment_context_length: int = Field(default=4096, ge=1)
    llm_candidate_ranking_context_length: int = Field(default=16384, ge=1)
    scheduler_interval_minutes: int = 1
    schedule_min_interval_minutes: int = 10
    schedule_max_interval_minutes: int = 30
    page_delay_min_seconds: int = 5
    page_delay_max_seconds: int = 15
    collector_headless: bool = False
    collector_page_queue_size: int = Field(default=2, ge=1)
    candidate_pool_size: int = Field(default=20, ge=1)
    initial_assessment_concurrency: int = Field(default=2, ge=1)
    max_task_retries: int = 3
    xianyu_state_dir: str = str(ROOT_DIR / "data" / "xianyu_states")
    xianyu_account_cooldown_minutes: int = 60
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
    # .env 和运行数据位于项目根目录；非敏感 YAML 配置位于 backend/config.yaml。
    config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    if config_path.exists():
        # YAML 只覆盖非敏感配置；密钥始终由环境变量提供。
        values = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        llm = values.get("llm", {})
        tasks = llm.get("tasks", {})
        providers = llm.get("providers", {})
        siliconflow = providers.get("siliconflow", {})
        settings = settings.model_copy(update={
            "siliconflow_base_url": siliconflow.get("base_url", settings.siliconflow_base_url),
            "llm_provider": llm.get("default_provider", settings.llm_provider),
            "llm_model": tasks.get("user_requirement", {}).get("model", settings.llm_model),
            "llm_retry_count": llm.get("retry_count", settings.llm_retry_count),
            "llm_timeout_seconds": llm.get("timeout_seconds", settings.llm_timeout_seconds),
            "ollama_base_url": values.get("ollama", {}).get("base_url", settings.ollama_base_url),
            "ollama_model": values.get("ollama", {}).get("model", settings.ollama_model),
            "llm_user_requirement_context_length": tasks.get(
                "user_requirement", {}).get("context_length", settings.llm_user_requirement_context_length),
            "llm_product_parse_context_length": tasks.get(
                "product_parse", {}).get("context_length", settings.llm_product_parse_context_length),
            "llm_item_assessment_context_length": tasks.get(
                "item_assessment", {}).get("context_length", settings.llm_item_assessment_context_length),
            "llm_candidate_ranking_context_length": tasks.get(
                "candidate_ranking", {}).get("context_length", settings.llm_candidate_ranking_context_length),
            "scheduler_interval_minutes": values.get("scheduler", {}).get("poll_interval_minutes", settings.scheduler_interval_minutes),
            "schedule_min_interval_minutes": values.get("scheduler", {}).get("min_interval_minutes", settings.schedule_min_interval_minutes),
            "schedule_max_interval_minutes": values.get("scheduler", {}).get("max_interval_minutes", settings.schedule_max_interval_minutes),
            "page_delay_min_seconds": values.get("collector", {}).get("page_delay_min_seconds", settings.page_delay_min_seconds),
            "page_delay_max_seconds": values.get("collector", {}).get("page_delay_max_seconds", settings.page_delay_max_seconds),
            "collector_headless": values.get("collector", {}).get("headless", settings.collector_headless),
            "collector_page_queue_size": values.get("collector", {}).get("page_queue_size", settings.collector_page_queue_size),
            "candidate_pool_size": values.get("analysis", {}).get("candidate_pool_size", settings.candidate_pool_size),
            "initial_assessment_concurrency": values.get(
                "analysis", {}).get("initial_assessment_concurrency", settings.initial_assessment_concurrency),
            "xianyu_state_dir": str((ROOT_DIR / values.get("xianyu", {}).get("state_dir", "data/xianyu_states")).resolve()),
            "xianyu_account_cooldown_minutes": values.get("xianyu", {}).get("account_cooldown_minutes", settings.xianyu_account_cooldown_minutes),
        })
    return settings
