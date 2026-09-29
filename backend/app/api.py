import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_session
from .llm.service import LLMService
from .llm.factory import ProviderFactory
from .models import NotificationLog, RuleRecipient, TaskRun, WatchRule, XianyuAccount
from .schemas import RuleCreate, RuleResponse, TaskResponse, XianyuAccountCreate, XianyuAccountResponse
from .services.xianyu_accounts import XianyuAccountService
from .services.task_runner import TaskRunner

router = APIRouter(prefix="/api")


def get_runner() -> TaskRunner:
    settings = get_settings()
    return TaskRunner(settings, LLMService(ProviderFactory.create(settings, settings.llm_provider), settings))


@router.get("/xianyu-accounts", response_model=list[XianyuAccountResponse])
def list_xianyu_accounts(session: Session = Depends(get_session)):
    return session.scalars(select(XianyuAccount).order_by(XianyuAccount.created_at.desc())).all()


@router.post("/xianyu-accounts", response_model=XianyuAccountResponse)
def create_xianyu_account(payload: XianyuAccountCreate, session: Session = Depends(get_session)):
    if session.scalar(select(XianyuAccount).where(XianyuAccount.name == payload.name)):
        raise HTTPException(409, "账号名称已存在")
    state_path = XianyuAccountService(get_settings()).state_path_for(payload.name)
    account = XianyuAccount(name=payload.name, state_path=str(state_path), status="needs_login")
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


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


def serialize_rule(rule: WatchRule) -> RuleResponse:
    """将 ORM 规则统一转换为前端编辑和状态展示需要的字段。"""
    return RuleResponse(
        id=rule.id, product=rule.product, extra_conditions=rule.extra_conditions, budget=rule.budget,
        exclude_keywords=rule.exclude_keywords or [], interval_minutes=rule.interval_minutes,
        is_enabled=rule.is_enabled, next_run_at=rule.next_run_at,
        emails=[item.email for item in rule.recipients], parsed_requirement=rule.parsed_requirement,
    )


@router.post("/rules", response_model=RuleResponse)
def create_rule(payload: RuleCreate, session: Session = Depends(get_session)):
    rule = WatchRule(product=payload.product, extra_conditions=payload.extra_conditions, budget=payload.budget,
                     exclude_keywords=payload.exclude_keywords, interval_minutes=payload.interval_minutes,
                     is_enabled=payload.enabled)
    rule.recipients = [RuleRecipient(email=str(email)) for email in payload.emails]
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
    rule.product, rule.extra_conditions, rule.budget = payload.product, payload.extra_conditions, payload.budget
    rule.exclude_keywords, rule.interval_minutes, rule.is_enabled = payload.exclude_keywords, payload.interval_minutes, payload.enabled
    rule.parsed_requirement = None
    rule.recipients.clear()
    rule.recipients.extend(RuleRecipient(email=str(email)) for email in payload.emails)
    session.commit()
    session.refresh(rule)
    return serialize_rule(rule)


@router.post("/rules/{rule_id}/run", response_model=TaskResponse)
async def run_rule(rule_id: str, session: Session = Depends(get_session)):
    if not session.get(WatchRule, rule_id):
        raise HTTPException(404, "规则不存在")
    task = TaskRun(rule_id=rule_id, status="pending", stage="pending")
    session.add(task)
    session.commit()
    asyncio.create_task(get_runner().run(rule_id, task.id))
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


@router.get("/rules/{rule_id}/tasks", response_model=list[TaskResponse])
def list_rule_tasks(rule_id: str, session: Session = Depends(get_session)):
    if not session.get(WatchRule, rule_id):
        raise HTTPException(404, "规则不存在")
    return session.scalars(select(TaskRun).where(TaskRun.rule_id == rule_id)
                           .order_by(TaskRun.created_at.desc())).all()


@router.get("/rules/{rule_id}/history")
def rule_history(rule_id: str, session: Session = Depends(get_session)):
    if not session.get(WatchRule, rule_id):
        raise HTTPException(404, "规则不存在")
    return [{"id": item.id, "status": item.status, "price": float(item.price), "email": item.email,
             "listing_id": item.listing_id, "created_at": item.created_at}
            for item in session.scalars(select(NotificationLog).where(NotificationLog.rule_id == rule_id)
                                       .order_by(NotificationLog.created_at.desc())).all()]
