"""导入批次相关的响应模型 —— 见《API接口设计》§7。"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.order import ImportErrorItem


class ImportBatchListItem(BaseModel):
    id: int
    filename: str
    uploaded_by: str | None = Field(default=None, description="上传人用户名")
    total_rows: int
    success_rows: int
    failed_rows: int
    status: str
    created_at: datetime
    finished_at: datetime | None = None


class ImportBatchDetail(ImportBatchListItem):
    """详情在列表字段基础上追加错误明细与关联订单。"""

    errors: list[ImportErrorItem] = Field(default_factory=list)
    order_ids: list[int] = Field(
        default_factory=list, description="该批次导入的订单 id，供『点批次跳订单列表』"
    )
