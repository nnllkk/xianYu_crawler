import json

from .provider import LLMProvider


class MockProvider(LLMProvider):
    """本地测试用 Provider，不访问网络也不产生模型费用。"""

    def __init__(self, settings: object) -> None:
        pass

    def complete_json(self, *, system: str, user: str, schema_name: str) -> str:
        payload = json.loads(user)
        if schema_name == "UserRequirement":
            product = payload["product"]
            conditions = [item.strip() for item in (payload.get("extra_conditions") or "").split("+") if item.strip()]
            return json.dumps({
                "keyword": product,
                "search_queries": [product, f"{product} 二手", f"{product} 高配"],
                "price_range": {"min": None, "max": None},
                "conditions": conditions,
                "exclude_keywords": payload.get("exclude_keywords") or [],
                "original_input": payload,
            }, ensure_ascii=False)
        if schema_name == "ProductConditions":
            text = f"{payload.get('title') or ''} {payload.get('card_text') or ''}".strip()
            return json.dumps({"xianyu_item_id": payload["xianyu_item_id"], "conditions": [text],
                               "extraction_status": "complete"}, ensure_ascii=False)
        if schema_name == "RankingResult":
            return json.dumps({"items": [{"xianyu_item_id": item["xianyu_item_id"], "recommended": True,
                                           "score": 50, "rank": index, "reason": "本地测试推荐",
                                           "risks": [], "uncertain": True}
                                          for index, item in enumerate(payload.get("candidates", []), start=1)]},
                              ensure_ascii=False)
        raise ValueError(f"MockProvider 不支持 Schema: {schema_name}")
