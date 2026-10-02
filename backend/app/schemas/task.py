"""任务相关的响应模型 —— 见《API接口设计》§9、§10.1。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import ReviewResult
from app.schemas.order import AiAnalysisBrief


class TaskOrderBrief(BaseModel):
    """任务上下文里的订单快照。

    §9.2（管理员看「这单到底要做什么」）和 §10.1（Worker 拿去填 ERP）共用一份 ——
    两边要的字段完全一致，拆成两个模型只会让它们慢慢漂移，最后一边缺一个字段。

    电话和地址**不脱敏**：管理员要拿它核对，Worker 要把它敲进 ERP 表单。
    全项目只有订单**列表页**需要遮，那里用 `mask_phone`。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_no: str
    customer_name: str
    phone: str
    address: str
    product_name: str
    sku: str
    quantity: int
    amount: str = Field(description="字符串，避免 DECIMAL 经 float 丢精度")
    buyer_message: str | None = None


class TaskExecutionItem(BaseModel):
    """一次 RPA 执行（含每次重试）—— §9.2 与 §9.6 共用。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    attempt: int
    status: str
    worker_name: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    screenshot_path: str | None = None


# ============================================================
# 列表 / 详情
# ============================================================


class TaskListItem(BaseModel):
    """§9.1 —— 刻意不带地址、买家留言这类长文本。

    列表一次可能拉几十条，把只有详情才用得上的字段塞进来纯属浪费带宽。
    """

    id: int
    order_id: int
    order_no: str
    customer_name: str
    priority: str
    status: str
    need_review: bool
    retry_count: int
    max_retry: int
    last_error: str | None = None
    claimed_by: str | None = None
    created_at: datetime


class TaskDetail(BaseModel):
    """§9.2。

    `analysis` 是**生成该任务时依据的那一次** AI 分析（`tasks.ai_analysis_id`），
    不是「该订单最新的一次」。两者通常相同，但人工修正后可能分叉 ——
    任务详情要能解释「当初为什么这么判」，所以不能取最新。
    """

    id: int
    order: TaskOrderBrief
    analysis: AiAnalysisBrief | None = None
    priority: str
    status: str
    need_review: bool
    retry_count: int
    max_retry: int
    last_error: str | None = None
    review_result: str | None = None
    review_reason: str | None = None
    cancel_reason: str | None = None
    claimed_by: str | None = None
    queued_at: datetime | None = None
    finished_at: datetime | None = None
    executions: list[TaskExecutionItem] = Field(default_factory=list)


# ============================================================
# 状态变更类接口的返回（§9.3 / §9.4 / §9.5）
# ============================================================


class TaskStatusChanged(BaseModel):
    """审核、取消的返回体：只回「变成什么了」。

    前端拿它直接更新本地那一行，不必为了看新状态再拉一次列表。
    """

    task_id: int
    status: str


class TaskRetryResult(TaskStatusChanged):
    """重试多回一个 `retry_count` —— 前端要立刻显示「第 2/3 次」。"""

    retry_count: int


# ============================================================
# 请求体（§9.3 / §9.4 / §9.5）
# ============================================================


class TaskReviewRequest(BaseModel):
    """`result` 用 `ReviewResult` 而不是 `str`：非法值由 Pydantic 挡在 422，
    service 里就不用再写一遍「APPROVED 还是 REJECTED」的判断。"""

    result: ReviewResult
    reason: str | None = Field(default=None, max_length=500, description="审核意见")


class TaskRetryRequest(BaseModel):
    reset_retry_count: bool = Field(
        default=False,
        description="把 retry_count 归零 —— 人工介入通常已解决根因，不该再消耗自动重试次数",
    )
    reason: str | None = Field(default=None, max_length=500, description="人工处置说明")


class TaskCancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500, description="取消原因")
