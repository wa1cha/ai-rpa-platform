"""tasks 表的数据访问。

状态机、状态流转的合法性判断全在 service —— 这里只负责「取出来」「放进去」，
外加「哪些订单还没有任务」这种为生成任务服务的查询。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, and_, or_, select

from app.core.enums import TaskStatus
from app.models.ai_analysis import AiAnalysis
from app.models.order import Order
from app.models.task import Task
from app.repositories.base import BaseRepository
from app.repositories.order_repository import latest_analysis_id_subquery
from app.schemas.common import PageParams


@dataclass(slots=True)
class TaskListRow:
    """任务列表一行：任务本身 + 从订单 JOIN 来的两个展示字段。"""

    task: Task
    order_no: str
    customer_name: str


@dataclass(slots=True)
class TaskFilters:
    """任务列表的筛选条件 —— 对应《API接口设计》§9.1 的两个查询参数。"""

    statuses: Sequence[str] | None = None
    priority: str | None = None


class TaskRepository(BaseRepository[Task]):
    model = Task

    # ---------- 列表 ----------

    def _list_stmt(self, filters: TaskFilters) -> Select:
        stmt = select(Task, Order.order_no, Order.customer_name).join(
            Order, Order.id == Task.order_id
        )

        conditions = []
        if filters.statuses:
            conditions.append(Task.status.in_(list(filters.statuses)))
        if filters.priority:
            conditions.append(Task.priority == filters.priority)
        if conditions:
            stmt = stmt.where(and_(*conditions))

        # 默认按创建时间倒序，和订单列表保持一致。
        #
        # 这里**刻意不按 priority 排序**：`priority` 是 VARCHAR，
        # MySQL 按字符串比，顺序会变成 HIGH < LOW < MEDIUM —— 正好是错的。
        # 想排对就得 `ORDER BY FIELD(...)`，那样又用不上
        # `idx_tasks_status_priority` 索引。真正的「按优先级出队」发生在
        # Redis 队列里（score 用的是数字权重），列表页按时间看反而更符合直觉。
        return stmt.order_by(Task.created_at.desc(), Task.id.desc())

    async def list_tasks(
        self, filters: TaskFilters, params: PageParams
    ) -> tuple[list[TaskListRow], int]:
        # Task JOIN Order 是 N:1，一个任务只对应一个订单，行数不膨胀，
        # 所以基类默认的「数结果行数」在这里是对的。
        rows, total = await self.paginate(self._list_stmt(filters), params)
        return [
            TaskListRow(task=row[0], order_no=row[1], customer_name=row[2]) for row in rows
        ], total

    # ---------- 详情 ----------

    async def get_with_order_and_analysis(
        self, task_id: int
    ) -> tuple[Task, Order, AiAnalysis | None] | None:
        """任务详情一次取回：任务 + 订单 + **该任务引用的那次**分析。

        注意 JOIN 条件用的是 `AiAnalysis.id == Task.ai_analysis_id`，
        而不是「最新一次分析」的子查询 —— 任务详情要回答的是「当初凭什么
        这么判」。人工修正过 AI 结果之后，这两个会分叉。
        """
        stmt = (
            select(Task, Order, AiAnalysis)
            .join(Order, Order.id == Task.order_id)
            .outerjoin(AiAnalysis, AiAnalysis.id == Task.ai_analysis_id)
            .where(Task.id == task_id)
        )
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None
        return row[0], row[1], row[2]

    # ---------- 生成任务 ----------

    async def list_orders_without_task(
        self, *, order_ids: Sequence[int] | None = None
    ) -> list[tuple[Order, AiAnalysis | None]]:
        """还没有任务的订单，顺带带出各自最新一次分析。

        用 `Task.id IS NULL` 反连接，而不是 `NOT IN (SELECT order_id FROM tasks)`：
        后者在子查询返回 NULL 时的语义容易踩坑，MySQL 对它的优化通常也不如反连接。

        不按订单状态过滤是**故意的**：Phase 3 还没有 AI，订单永远停不到
        `ANALYZED`，卡状态的话这个接口今天就是死的。真要限流由调用方传
        `order_ids` 自己控制。
        """
        stmt = (
            select(Order, AiAnalysis)
            .outerjoin(AiAnalysis, AiAnalysis.id == latest_analysis_id_subquery())
            .outerjoin(Task, Task.order_id == Order.id)
            .where(Task.id.is_(None))
            .order_by(Order.created_at.asc(), Order.id.asc())
        )
        if order_ids:
            stmt = stmt.where(Order.id.in_(list(order_ids)))

        rows = (await self.session.execute(stmt)).all()
        return [(row[0], row[1]) for row in rows]

    # ---------- 僵尸任务回收 ----------

    async def list_stale_running(self, cutoff: datetime) -> list[Task]:
        """心跳早于 `cutoff` 的 RUNNING 任务 —— 判定 Worker 已失联。

        心跳为 NULL 也算：正常路径上 claim 一定会写 heartbeat_at，
        真出现 NULL 说明这行是别的途径置成 RUNNING 的，它永远等不到心跳，
        不收就会一直卡着 —— 那比误收一次更糟。

        走 `idx_tasks_status`，每分钟跑一次、命中行数通常为 0。
        """
        stmt = select(Task).where(
            Task.status == TaskStatus.RUNNING,
            or_(Task.heartbeat_at.is_(None), Task.heartbeat_at < cutoff),
        )
        return list(await self.session.scalars(stmt))

    # ---------- 队列对账 ----------

    async def list_queued_for_reconcile(self) -> list[tuple[int, str, datetime | None]]:
        """重建队列所需的全部事实：`(task_id, priority, queued_at)`。

        只取 QUEUED 的 —— MySQL 的 `tasks.status` 是队列的唯一事实来源，
        Redis 里的东西随时可以丢，丢了就照这里重建。
        """
        rows = await self.session.execute(
            select(Task.id, Task.priority, Task.queued_at).where(
                Task.status == TaskStatus.QUEUED
            )
        )
        return [(row[0], row[1], row[2]) for row in rows]
