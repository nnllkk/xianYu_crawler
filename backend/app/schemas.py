from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class RuleCreate(BaseModel):
    product: str = Field(min_length=1, max_length=255)
    extra_conditions: str | None = None
    budget: str | None = None
    emails: list[EmailStr] = Field(min_length=1)
    interval_minutes: int = Field(default=15, ge=10, le=30)
    max_pages: int = Field(default=20, ge=1, le=20)
    enabled: bool = True


class RuleResponse(BaseModel):
    id: str
    product: str
    extra_conditions: str | None
    budget: str | None
    interval_minutes: int
    max_pages: int
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


class XianyuAccountCreate(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


class XianyuAccountResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    name: str
    status: str
    failure_count: int
    last_error: str | None
    cooldown_until: datetime | None
    last_used_at: datetime | None
    is_enabled: bool
    state_file: str
    state_exists: bool
