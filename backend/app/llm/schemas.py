from pydantic import BaseModel, Field, model_validator


class PriceRange(BaseModel):
    min: float | None = Field(default=None, ge=0)
    max: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def check_bounds(self) -> "PriceRange":
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("price_range.min 不能大于 max")
        return self


class OriginalInput(BaseModel):
    product: str
    extra_conditions: str | None = None
    budget: str | None = None


class UserRequirement(BaseModel):
    keyword: str
    search_query: str = Field(min_length=1)
    price_range: PriceRange = Field(default_factory=PriceRange)
    conditions: list[str] = Field(default_factory=list)
    original_input: OriginalInput


class ItemAssessment(BaseModel):
    """单商品是否进入后续排序候选池的判断结果。"""

    xianyu_item_id: str
    worthwhile: bool
    reason: str
    risks: list[str] = Field(default_factory=list)
    uncertain: bool = False


class RankedItem(BaseModel):
    xianyu_item_id: str
    recommended: bool = True
    score: float = Field(ge=0, le=100)
    rank: int = Field(ge=1)
    reason: str
    risks: list[str] = Field(default_factory=list)
    uncertain: bool = False


class RankingResult(BaseModel):
    items: list[RankedItem] = Field(default_factory=list)
