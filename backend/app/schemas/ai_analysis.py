"""AI 分析相关的响应/请求模型 —— 见《API接口设计》§8。"""

from datetime import datetime

from pydantic import BaseModel, Field


class AiAnalysisListItem(BaseModel):
    """§8.1 —— 一条分析记录。带 `buyer_message` 是因为「为什么这么判」要对着留言看。"""

    id: int
    order_id: int
    order_no: str
    buyer_message: str | None = None
    priority: str | None = None
    deadline: str | None = None
    need_contact: bool | None = None
    risk_level: str | None = None
    risk_reason: str | None = None
    action: str | None = None
    model_name: str | None = None
    status: str
    created_at: datetime


# ============================================================
# §8.2 人工修正
# ============================================================


class AiAnalysisReviewChange(BaseModel):
    """一次字段修正。`new_value` 用字符串承载 —— `need_contact` 会传 "false"，
    由 service 按字段类型各自解析，避免请求体里混三种 JSON 类型。"""

    field_name: str
    new_value: str


class AiAnalysisReviewRequest(BaseModel):
    changes: list[AiAnalysisReviewChange] = Field(min_length=1)
    reason: str | None = Field(default=None, max_length=500, description="修正原因")


class AiAnalysisReviewApplied(BaseModel):
    """实际落到某字段上的修改（原值/新值都给，便于前端展示与回溯）。"""

    field_name: str
    original_value: str | None = None
    new_value: str | None = None


class AiAnalysisReviewResult(BaseModel):
    analysis_id: int
    applied: list[AiAnalysisReviewApplied] = Field(default_factory=list)
    task_updated: bool = Field(
        description="任务仍处 WAITING_REVIEW 时才同步其 priority；否则只记录、不改任务"
    )
