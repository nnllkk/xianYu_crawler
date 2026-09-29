from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class RuleCreate(BaseModel):
    product: str = Field(min_length=1, max_length=255)
    extra_conditions: str | None = None
    budget: str | None = None
    exclude_keywords: list[str] = Field(default_factory=list)
    emails: list[EmailStr] = Field(min_length=1)
    interval_minutes: int = Field(default=15, ge=10, le=30)
    enabled: bool = True


class RuleResponse(BaseModel):
    id: str
    product: str
    extra_conditions: str | None
    budget: str | None
    exclude_keywords: list[str]
    interval_minutes: int
    is_enabled: bool
    next_run_at: datetime | None
    emails: list[str]
    parsed_requirement: dict | None


class TaskResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    rule_id: str
    status: str
    stage: str
    scraped_count: int
    candidate_count: int
    sent_count: int
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
