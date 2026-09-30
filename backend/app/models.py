from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def beijing_now() -> datetime:
    """返回用于 MySQL 无时区 DATETIME 字段的北京时间。"""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def to_beijing_naive(value: datetime) -> datetime:
    """将采集器的带时区时间统一转换为北京时间后再写入 DATETIME。"""
    if value.tzinfo is None:
        return value
    return value.astimezone(BEIJING_TZ).replace(tzinfo=None)


class WatchRule(Base):
    __tablename__ = "watch_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    product: Mapped[str] = mapped_column(String(255))
    extra_conditions: Mapped[str | None] = mapped_column(Text)
    budget: Mapped[str | None] = mapped_column(String(64))
    parsed_requirement: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    interval_minutes: Mapped[int] = mapped_column(default=15)
    max_pages: Mapped[int] = mapped_column(default=20)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=beijing_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=beijing_now, onupdate=beijing_now)
    recipients: Mapped[list[RuleRecipient]] = relationship(back_populates="rule", cascade="all, delete-orphan")


class RuleRecipient(Base):
    __tablename__ = "rule_recipients"
    __table_args__ = (UniqueConstraint("rule_id", "email", name="uq_rule_recipient"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    rule_id: Mapped[str] = mapped_column(ForeignKey("watch_rules.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(320))
    rule: Mapped[WatchRule] = relationship(back_populates="recipients")


class XianyuAccount(Base):
    """闲鱼登录态索引；敏感 Cookie 只存储在本地 storage_state JSON 文件中。"""

    __tablename__ = "xianyu_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(128), unique=True)
    state_path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="needs_login")
    failure_count: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=beijing_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=beijing_now, onupdate=beijing_now)


class Listing(Base):
    __tablename__ = "listings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    xianyu_item_id: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(Text)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    url: Mapped[str] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    seller_location: Mapped[str | None] = mapped_column(String(255))
    matched_search_queries: Mapped[list[str]] = mapped_column(JSON, default=list)
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    conditions: Mapped[list[str] | None] = mapped_column(JSON)
    extraction_status: Mapped[str] = mapped_column(String(32), default="pending")
    extracted_text_hash: Mapped[str | None] = mapped_column(String(64))
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=beijing_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=beijing_now, onupdate=beijing_now)


class TaskRun(Base):
    __tablename__ = "task_runs"
    __table_args__ = (Index("ix_task_runs_rule_created", "rule_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    rule_id: Mapped[str] = mapped_column(ForeignKey("watch_rules.id", ondelete="CASCADE"))
    xianyu_account_id: Mapped[str | None] = mapped_column(ForeignKey("xianyu_accounts.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    stage: Mapped[str] = mapped_column(String(32), default="pending")
    search_queries: Mapped[list[str] | None] = mapped_column(JSON)
    scraped_count: Mapped[int] = mapped_column(default=0)
    candidate_count: Mapped[int] = mapped_column(default=0)
    sent_count: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=beijing_now)


class NotificationState(Base):
    __tablename__ = "notification_states"
    __table_args__ = (UniqueConstraint("rule_id", "listing_id", "email", name="uq_notification_state"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    rule_id: Mapped[str] = mapped_column(ForeignKey("watch_rules.id", ondelete="CASCADE"))
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(320))
    last_sent_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    last_sent_at: Mapped[datetime] = mapped_column(DateTime, default=beijing_now)


class NotificationLog(Base):
    __tablename__ = "notification_logs"
    __table_args__ = (Index("ix_notification_logs_task", "task_run_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    task_run_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"))
    rule_id: Mapped[str] = mapped_column(ForeignKey("watch_rules.id", ondelete="CASCADE"))
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(320))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=beijing_now)
