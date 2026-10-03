"""订单相关的响应模型 —— 见《API接口设计》§6。"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


def amount_to_str(amount: Decimal) -> str:
    """金额按字符串给前端。

    DECIMAL 转 float 会丢精度（`299.00` 变成 `299.0`），转字符串则原样保留
    两位小数。金额用字符串传是金融系统的惯例 —— 前端要算再自己转，
    传输过程中不能有任何精度损失。
    """
    return f"{amount:.2f}"


def mask_phone(phone: str) -> str:
    """列表页手机号脱敏：`13800008888` → `138****8888`。

    脱敏放在**响应组装**这一步，而不是查询时 —— 数据库里存的必须是完整值，
    否则客服想回拨都拨不了。只有「给列表页看」这个场景才需要遮。
    """
    if len(phone) < 7:
        return phone
    return f"{phone[:3]}****{phone[-4:]}"


# ============================================================
# 导入
# ============================================================


class ImportErrorItem(BaseModel):
    """一行导入失败的原因。`row` 是**文件里的行号**（表头算第 1 行）。"""

    row: int
    order_no: str | None = None
    reason: str


class OrderImportResult(BaseModel):
    batch_id: int | None = Field(
        default=None, description="dry_run 时为 null，因为不产生批次记录"
    )
    filename: str
    total_rows: int
    success_rows: int
    failed_rows: int
    status: str
    errors: list[ImportErrorItem] = Field(default_factory=list)


# ============================================================
# 重新触发 AI 分析（§6.4）
# ============================================================


class OrderReanalyzeRequest(BaseModel):
    """重分析的请求体，**可整个省略**（不带 body 也能调）。

    `reason` 只记日志、不落库：一次重分析在 `ai_analyses` 新增一行本身就是记录，
    这里只是给日志/审计留一句人话，不值得为它新增表或列。
    """

    reason: str | None = Field(default=None, max_length=500)


class OrderReanalyzeResult(BaseModel):
    order_id: int
    status: str = Field(
        description="重分析后订单回到 IMPORTED（待分析），由 AI Worker 出队后置 ANALYZING"
    )


# ============================================================
# 列表 / 详情
# ============================================================


class OrderListItem(BaseModel):
    id: int
    order_no: str
    platform: str
    ordered_at: datetime
    customer_name: str
    phone: str = Field(description="列表页脱敏")
    product_name: str
    sku: str
    quantity: int
    amount: str = Field(description="字符串，避免 DECIMAL 经 float 丢精度")
    status: str
    risk_level: str | None = None
    priority: str | None = None
    task_id: int | None = None
    task_status: str | None = None


class AiAnalysisBrief(BaseModel):
    """一次 AI 分析的摘要。

    订单详情用它表示「最新一次」，任务详情用它表示「生成该任务时依据的那次」——
    同一个结构、两种语义，所以名字不能叫 Latest，否则任务那边一读就别扭。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    priority: str | None = None
    deadline: str | None = None
    need_contact: bool | None = None
    risk_level: str | None = None
    risk_reason: str | None = None
    action: str | None = None
    model_name: str | None = None
    created_at: datetime


class TaskBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    priority: str
    retry_count: int
    max_retry: int
    created_at: datetime


class ReviewLogItem(BaseModel):
    field_name: str
    original_value: str | None = None
    new_value: str | None = None
    reviewer: str = Field(description="修正人用户名")
    reviewed_at: datetime


class OrderDetail(BaseModel):
    id: int
    order_no: str
    platform: str
    ordered_at: datetime
    customer_name: str
    phone: str = Field(description="详情页返回完整手机号")
    address: str
    product_name: str
    sku: str
    quantity: int
    amount: str
    buyer_message: str | None = None
    seller_note: str | None = None
    status: str
    imported_at: datetime
    latest_analysis: AiAnalysisBrief | None = None
    task: TaskBrief | None = None
    review_logs: list[ReviewLogItem] = Field(default_factory=list)
