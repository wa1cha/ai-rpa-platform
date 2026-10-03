"""通知相关的响应模型 —— 见《API接口设计》§12。

字段严格按 §12 契约，**不加 `related_order_id` / `error_message` 这类**：
表里有、但接口没承诺的字段一律不外露，免得前端依赖上它们、以后想改表就动不了。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class NotificationListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: str
    channel: str
    title: str | None = None
    content: str | None = None
    status: str
    related_task_id: int | None = None
    created_at: datetime
    sent_at: datetime | None = None
