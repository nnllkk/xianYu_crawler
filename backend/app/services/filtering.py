import re
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Listing, NotificationState, WatchRule


def parse_budget(value: str | None) -> tuple[Decimal | None, Decimal | None]:
    if not value:
        return None, None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)?\s*-\s*(\d+(?:\.\d+)?)?\s*", value)
    if not match:
        raise ValueError("预算必须是 min-max、min- 或 -max 格式")
    return (Decimal(match.group(1)) if match.group(1) else None,
            Decimal(match.group(2)) if match.group(2) else None)


def deterministic_filter(session: Session, rule: WatchRule, items: list[dict], recipients: list[str]) -> list[Listing]:
    minimum, maximum = parse_budget(rule.budget)
    candidates: list[Listing] = []
    for item in items:
        price = item.get("price")
        item_id = item.get("xianyu_item_id")
        if not item_id or price is None or not item.get("url"):
            continue
        price_decimal = Decimal(str(price))
        if minimum is not None and price_decimal < minimum or maximum is not None and price_decimal > maximum:
            continue
        searchable = f"{item.get('title') or ''} {(item.get('raw_data') or {}).get('card_text') or ''}".lower()
        if any(word.lower() in searchable for word in (rule.exclude_keywords or [])):
            continue
        existing = session.scalar(select(Listing).where(Listing.xianyu_item_id == item_id))
        states = session.scalars(select(NotificationState).where(
            NotificationState.rule_id == rule.id,
            NotificationState.listing_id == existing.id if existing else False,
            NotificationState.email.in_(recipients),
        )).all()
        sent_prices = {state.email: state.last_sent_price for state in states}
        # 每个邮箱独立判断：新邮箱仍应收到商品，只有所有目标邮箱都已被同价/涨价覆盖时才排除。
        if recipients and all(email in sent_prices and price_decimal >= sent_prices[email] for email in recipients):
            continue
        candidates.append(Listing(**item))
    return sorted(candidates, key=lambda item: item.price or Decimal("Infinity"))
