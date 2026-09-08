"""标签的唯一类型定义；独立于模型和 CSV IO。"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class InsightAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    detail: str = Field(min_length=1)
    evidence_type: Literal["实际体验", "预期", "泛述"]
    journey: Literal["购买前", "购买中", "购买后"] | None = None
    topic: Literal["尺码与贴合", "舒适度", "面料", "耐穿性", "设计与功能", "价格与价值", "交付与服务", "整体评价"] | None = None
    sentiment: Literal["正面", "负面", "中性", "褒贬并存"] | None = None
    scene: list[Literal["日常穿着", "工作通勤", "运动步行", "居家睡眠", "送礼"]] | None = None
    context: str | None = None
    subject: str | None = None
    motivation: str | None = None
    content_interest: str | None = None
    purchase_focus: str | None = None
    barrier: str | None = None
    competitor: str | None = None
    comparison_reason: str | None = None
    choice_status: str | None = None
    behavior: Literal["退换", "复购", "不再购买"] | None = None
    behavior_status: Literal["意向", "已发生"] | None = None
    behavior_reason: str | None = None
    cause: str | None = None
    impact: str | None = None
    unmet_need: str | None = None

    @model_validator(mode="after")
    def check_links(self):
        if not self.detail.strip():
            raise ValueError("detail must not be blank")
        if self.sentiment and not self.topic:
            raise ValueError("sentiment requires topic")
        if bool(self.behavior) != bool(self.behavior_status):
            raise ValueError("behavior and behavior_status must occur together")
        if self.behavior_reason and not self.behavior:
            raise ValueError("behavior_reason requires behavior")
        if (self.comparison_reason or self.choice_status) and not self.competitor:
            raise ValueError("comparison requires competitor")
        return self


class ReviewInsight(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    extraction_class: Literal["review_insight"]
    extraction_text: str = Field(min_length=1)
    attributes: InsightAttributes
