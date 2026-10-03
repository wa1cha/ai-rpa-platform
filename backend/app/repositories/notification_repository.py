"""notifications 表的数据访问 —— 见《数据库设计》§4.8。

写入路径在 `NotificationService.notify_task_failed`（业务侧触发）；
后台的「通知列表」是只读的，查询方法就是下面这个 `list_notifications`。
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select

from app.models.notification import Notification
from app.repositories.base import BaseRepository
from app.schemas.common import PageParams


@dataclass(slots=True)
class NotificationFilters:
    """通知列表的筛选条件。`status` 是唯一能筛的列（§12 只有这一个查询参数）。"""

    status: str | None = None


class NotificationRepository(BaseRepository[Notification]):
    model = Notification

    async def list_notifications(
        self, filters: NotificationFilters, params: PageParams
    ) -> tuple[Sequence[Notification], int]:
        """通知流水，按创建时间倒序。

        排序加 `id DESC` 是为了让同一秒内的多条有稳定顺序 —— 否则翻页时
        可能出现「第 1 页和第 2 页看到同一条」。`status` 过滤走
        `idx_notifications_status`。
        """
        stmt = select(Notification).order_by(
            Notification.created_at.desc(), Notification.id.desc()
        )
        if filters.status:
            stmt = stmt.where(Notification.status == filters.status)
        return await self.paginate(stmt, params)
