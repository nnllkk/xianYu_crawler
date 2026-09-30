import asyncio
import logging
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_session
from .llm.service import LLMService
from .llm.factory import ProviderFactory
from .llm.schemas import UserRequirement
from .models import NotificationLog, RuleRecipient, TaskRun, WatchRule, XianyuAccount
from .schemas import RuleCreate, RuleResponse, TaskResponse, XianyuAccountCreate, XianyuAccountResponse
from .services.xianyu_accounts import XianyuAccountService
from .services.task_runner import TaskRunner

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


def _run_manual_task(rule_id: str, task_id: str) -> None:
    """在独立线程和事件循环执行采集，避免 Playwright 阻塞 FastAPI 请求循环。"""
    try:
        asyncio.run(get_runner().run(rule_id, task_id))
    except Exception:
        logger.exception("manual task failed", extra={"rule_id": rule_id, "task_id": task_id})


def get_runner() -> TaskRunner:
    settings = get_settings()
    providers = ProviderFactory.create_task_providers(settings)
    return TaskRunner(settings, LLMService(providers[settings.llm_provider], settings, providers))


def parse_rule_requirement(payload: RuleCreate) -> UserRequirement:
    """在保存规则前完成需求解析，确保首次任务可直接使用缓存。"""
    settings = get_settings()
    try:
        providers = ProviderFactory.create_task_providers(settings)
        return LLMService(providers[settings.llm_provider], settings, providers).parse_requirement(
            payload.product,
            payload.extra_conditions,
            payload.budget,
        )
    except (RuntimeError, ValidationError, ValueError) as exc:
        raise HTTPException(502, f"需求解析失败，规则未保存：{exc}") from exc


def serialize_xianyu_account(account: XianyuAccount) -> dict:
    return {
        "id": account.id, "name": account.name, "status": account.status,
        "failure_count": account.failure_count, "last_error": account.last_error,
        "cooldown_until": account.cooldown_until, "last_used_at": account.last_used_at,
        "is_enabled": account.is_enabled, "state_file": Path(account.state_path).name,
        "state_exists": Path(account.state_path).is_file(),
    }


@router.get("/xianyu-accounts", response_model=list[XianyuAccountResponse])
def list_xianyu_accounts(session: Session = Depends(get_session)):
    accounts_service = XianyuAccountService(get_settings())
    if accounts_service.release_expired_cooldowns(session):
        session.commit()
    accounts = session.scalars(select(XianyuAccount).order_by(XianyuAccount.created_at.desc())).all()
    return [serialize_xianyu_account(account) for account in accounts]


@router.post("/xianyu-accounts", response_model=XianyuAccountResponse)
def create_xianyu_account(payload: XianyuAccountCreate, session: Session = Depends(get_session)):
    if session.scalar(select(XianyuAccount).where(XianyuAccount.name == payload.name)):
        raise HTTPException(409, "账号名称已存在")
    state_path = XianyuAccountService(get_settings()).state_path_for(payload.name)
    account = XianyuAccount(name=payload.name, state_path=str(state_path), status="needs_login")
    session.add(account)
    session.commit()
    session.refresh(account)
    account_id = account.id
    threading.Thread(
        target=lambda: asyncio.run(XianyuAccountService(get_settings()).capture_login_state(account_id)),
        name=f"xianyu-login-{account_id[:8]}", daemon=True,
    ).start()
    return serialize_xianyu_account(account)


@router.post("/xianyu-accounts/{account_id}/enable")
def enable_xianyu_account(account_id: str, session: Session = Depends(get_session)):
    account = session.get(XianyuAccount, account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    account.is_enabled = True
    session.commit()
    return {"id": account.id, "is_enabled": True}


@router.post("/xianyu-accounts/{account_id}/disable")
def disable_xianyu_account(account_id: str, session: Session = Depends(get_session)):
    account = session.get(XianyuAccount, account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    account.is_enabled = False
    session.commit()
    return {"id": account.id, "is_enabled": False}


@router.delete("/xianyu-accounts/{account_id}")
def delete_xianyu_account(account_id: str, session: Session = Depends(get_session)):
    account = session.get(XianyuAccount, account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    state_path = Path(account.state_path).resolve()
    state_root = Path(get_settings().xianyu_state_dir).resolve()
    if state_path.parent != state_root:
        raise HTTPException(400, "登录状态文件路径不在允许删除的目录内")
    if state_path.is_file():
        state_path.unlink()
    session.delete(account)
    session.commit()
    return {"id": account_id, "deleted": True}


def serialize_rule(rule: WatchRule) -> RuleResponse:
    """将 ORM 规则统一转换为前端编辑和状态展示需要的字段。"""
    return RuleResponse(
        id=rule.id, product=rule.product, extra_conditions=rule.extra_conditions, budget=rule.budget,
        interval_minutes=rule.interval_minutes, max_pages=rule.max_pages,
        is_enabled=rule.is_enabled, next_run_at=rule.next_run_at,
        emails=[item.email for item in rule.recipients], parsed_requirement=rule.parsed_requirement,
    )


def sync_rule_recipients(rule: WatchRule, emails: list[str]) -> None:
    """仅变更收件人差异，避免同邮箱更新时触发唯一键冲突。"""
    desired_emails = list(dict.fromkeys(emails))
    desired_set = set(desired_emails)
    existing_by_email = {recipient.email: recipient for recipient in rule.recipients}

    rule.recipients[:] = [
        recipient for recipient in rule.recipients if recipient.email in desired_set
    ]
    for email in desired_emails:
        if email not in existing_by_email:
            rule.recipients.append(RuleRecipient(email=email))


@router.post("/rules", response_model=RuleResponse)
def create_rule(payload: RuleCreate, session: Session = Depends(get_session)):
    requirement = parse_rule_requirement(payload)
    rule = WatchRule(product=payload.product, extra_conditions=payload.extra_conditions, budget=payload.budget,
                     parsed_requirement=requirement.model_dump(mode="json"), interval_minutes=payload.interval_minutes,
                     max_pages=payload.max_pages,
                     is_enabled=payload.enabled)
    sync_rule_recipients(rule, [str(email) for email in payload.emails])
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return serialize_rule(rule)


@router.get("/rules", response_model=list[RuleResponse])
def list_rules(session: Session = Depends(get_session)):
    return [serialize_rule(rule) for rule in session.scalars(select(WatchRule)).all()]


@router.put("/rules/{rule_id}", response_model=RuleResponse)
def update_rule(rule_id: str, payload: RuleCreate, session: Session = Depends(get_session)):
    rule = session.get(WatchRule, rule_id)
    if not rule:
        raise HTTPException(404, "规则不存在")
    requirement = parse_rule_requirement(payload)
    rule.product, rule.extra_conditions, rule.budget = payload.product, payload.extra_conditions, payload.budget
    rule.interval_minutes, rule.max_pages, rule.is_enabled = payload.interval_minutes, payload.max_pages, payload.enabled
    rule.parsed_requirement = requirement.model_dump(mode="json")
    sync_rule_recipients(rule, [str(email) for email in payload.emails])
    session.commit()
    session.refresh(rule)
    return serialize_rule(rule)


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: str, session: Session = Depends(get_session)):
    rule = session.get(WatchRule, rule_id)
    if not rule:
        raise HTTPException(404, "规则不存在")
    # 先发出停止信号，避免已开始的任务继续为即将删除的规则写入记录。
    TaskRunner.request_stop(rule_id)
    session.delete(rule)
    session.commit()
    return {"id": rule_id, "deleted": True}


@router.post("/rules/{rule_id}/run", response_model=TaskResponse)
async def run_rule(rule_id: str, session: Session = Depends(get_session)):
    if not session.get(WatchRule, rule_id):
        raise HTTPException(404, "规则不存在")
    if rule_id in TaskRunner._running:
        raise HTTPException(409, "该规则已有任务正在执行，请等待当前任务结束")
    task = TaskRun(rule_id=rule_id, status="pending", stage="pending")
    session.add(task)
    session.commit()
    threading.Thread(target=_run_manual_task, args=(rule_id, task.id),
                      name=f"manual-task-{task.id[:8]}", daemon=True).start()
    return task


@router.post("/rules/{rule_id}/stop")
def stop_rule(rule_id: str, session: Session = Depends(get_session)):
    rule = session.get(WatchRule, rule_id)
    if not rule:
        raise HTTPException(404, "规则不存在")
    rule.is_enabled = False
    rule.next_run_at = None
    TaskRunner.request_stop(rule_id)
    session.commit()
    return {"id": rule.id, "is_enabled": False}


@router.post("/rules/{rule_id}/start")
def start_rule(rule_id: str, session: Session = Depends(get_session)):
    rule = session.get(WatchRule, rule_id)
    if not rule:
        raise HTTPException(404, "规则不存在")
    rule.is_enabled = True
    rule.next_run_at = None
    session.commit()
    return {"id": rule.id, "is_enabled": True}


@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: str, session: Session = Depends(get_session)):
    task = session.get(TaskRun, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@router.get("/rules/{rule_id}/tasks")
def list_rule_tasks(rule_id: str, page: int = Query(1, ge=1), page_size: int = Query(8, ge=1, le=50),
                    session: Session = Depends(get_session)):
    if not session.get(WatchRule, rule_id):
        raise HTTPException(404, "规则不存在")
    base = select(TaskRun).where(TaskRun.rule_id == rule_id).order_by(TaskRun.created_at.desc())
    # 使用独立 count 查询，避免把全部历史记录加载到内存后再切片。
    from sqlalchemy import func
    total = session.scalar(select(func.count()).select_from(TaskRun).where(TaskRun.rule_id == rule_id)) or 0
    items = session.scalars(base.offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/rules/{rule_id}/history")
def rule_history(rule_id: str, session: Session = Depends(get_session)):
    if not session.get(WatchRule, rule_id):
        raise HTTPException(404, "规则不存在")
    return [{"id": item.id, "status": item.status, "price": float(item.price), "email": item.email,
             "listing_id": item.listing_id, "created_at": item.created_at}
            for item in session.scalars(select(NotificationLog).where(NotificationLog.rule_id == rule_id)
                                       .order_by(NotificationLog.created_at.desc())).all()]
