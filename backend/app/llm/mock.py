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
                "search_query": product,
                "price_range": {"min": None, "max": None},
                "conditions": conditions,
                "original_input": payload,
            }, ensure_ascii=False)
        if schema_name == "ItemAssessment":
            candidate = payload["candidate"]
            return json.dumps({"xianyu_item_id": candidate["xianyu_item_id"], "worthwhile": True,
                               "reason": "本地测试通过", "risks": [], "uncertain": True}, ensure_ascii=False)
        if schema_name == "RankingResult":
            return json.dumps({"items": [{"xianyu_item_id": item["xianyu_item_id"], "recommended": True,
                                           "score": 50, "rank": index, "reason": "本地测试推荐",
                                           "risks": [], "uncertain": True}
                                          for index, item in enumerate(payload.get("candidates", []), start=1)]},
                              ensure_ascii=False)
        raise ValueError(f"MockProvider 不支持 Schema: {schema_name}")
