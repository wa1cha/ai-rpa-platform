"""通知服务 —— v1 只落库 + 打日志，不真实外发。见《数据库设计》§4.8。

表结构按多渠道的样子建好了（`channel` / `target` / `status`），这里的接口
也按同样的形状留：以后接企业微信 / 钉钉，只需加一个渠道实现并在 `channel`
上分派，不动表、也不动调用方。
"""

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    NotificationChannel,
    NotificationStatus,
    NotificationType,
)
from app.models.notification import Notification
from app.models.task import Task
from app.repositories.notification_repository import (
    NotificationFilters,
    NotificationRepository,
)
from app.schemas.common import Page, PageParams
from app.schemas.notification import NotificationListItem

logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.notifications = NotificationRepository(session)

    def notify_task_failed(self, task: Task, error_message: str | None) -> Notification:
        """任务重试超限、最终判 `FAILED` 时记一条通知。

        两个刻意的选择：

        · **不 commit，只 add。** 调用方（`RpaService.submit_result`）正处在一个
          事务里，通知必须和任务状态一起提交或一起回滚。单独 commit 会造出
          「任务还是 RUNNING，通知却已经躺在表里」这种没人解释得清的中间态。
        · **`status=SENT` 而不是 `PENDING`。** v1 唯一的渠道是 LOG，
          而 LOG 是同步的：写下这行日志就算送达了。留一堆永远 PENDING 的行，
          等于让状态字段说谎。`PENDING` 是留给以后真实异步渠道的初始态。
        """
        record = Notification(
            type=NotificationType.TASK_FAILED.value,
            channel=NotificationChannel.LOG.value,
            title=f"任务 #{task.id} 最终失败"
            f"（已重试 {task.retry_count}/{task.max_retry} 次）",
            content=error_message or "无错误信息",
            status=NotificationStatus.SENT.value,
            related_order_id=task.order_id,
            related_task_id=task.id,
            sent_at=datetime.now(),
        )
        self.session.add(record)
        logger.warning(
            "通知[TASK_FAILED] task_id=%s order_id=%s：%s",
            task.id,
            task.order_id,
            error_message or "无错误信息",
        )
        return record

    # ---------- 查询（见《API接口设计》§12）----------

    async def list_notifications(
        self, params: PageParams, *, status: str | None = None
    ) -> Page[NotificationListItem]:
        """只读列表 —— 与 `notify_task_failed` 的写入路径完全分开。

        v1 只有 LOG 渠道、且写下即 `SENT`，所以默认**不带 status 过滤**
        才看得到东西；`?status=PENDING` 会返回空页，这是符合事实的
        （没有真实异步渠道，就没有 PENDING 的行）。
        """
        rows, total = await self.notifications.list_notifications(
            NotificationFilters(status=status), params
        )
        # `paginate` 返回的是 `Row`（每行一个元组），这里查的只有一列，取 `row[0]`。
        items = [NotificationListItem.model_validate(row[0]) for row in rows]
        return Page(
            items=items, total=total, page=params.page, page_size=params.page_size
        )
