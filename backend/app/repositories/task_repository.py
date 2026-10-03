"""tasks 表的数据访问。

状态机、状态流转的合法性判断全在 service —— 这里只负责「取出来」「放进去」，
外加「哪些订单还没有任务」这种为生成任务服务的查询。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, and_, delete, or_, select

from app.core.enums import OrderStatus, TaskStatus
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

    # ---------- 按订单取任务 ----------

    async def get_by_order_id(self, order_id: int) -> Task | None:
        """一单一任务（`tasks.order_id` 有唯一键），所以至多返回一条。"""
        stmt = select(Task).where(Task.order_id == order_id)
        return await self.session.scalar(stmt)

    async def delete_by_order_id_if_status_in(
        self, order_id: int, statuses: Sequence[TaskStatus]
    ) -> bool:
        """条件删除：只删处于给定状态的、该订单的任务，返回是否真的删掉了一行。

        重新触发 AI 分析时用它 —— 旧任务必须消失，`generate()` 的反连接
        （`Task.id IS NULL`）才会给这单重建任务（见 `list_orders_without_task`）。

        **条件写进 DELETE 本身**，而不是「先 `get` 再 `delete`」：后者是
        读-然后-写，读到的状态与删除时可能已不同（TOCTOU）—— 万一这中间任务被
        认领成 RUNNING，就会把一个正在跑的任务删掉。
        `rowcount` 就是「删不删得掉」的答案，与 `cas_transition_status` 同一手法。
        """
        stmt = delete(Task).where(
            Task.order_id == order_id,
            Task.status.in_([s.value for s in statuses]),
        )
        result = await self.session.execute(stmt)
        return result.rowcount > 0

    # ---------- 生成任务 ----------

    async def list_orders_without_task(
        self,
        *,
        order_ids: Sequence[int] | None = None,
        statuses: Sequence[OrderStatus] | None = None,
    ) -> list[tuple[Order, AiAnalysis | None]]:
        """还没有任务的订单，顺带带出各自最新一次分析。

        用 `Task.id IS NULL` 反连接，而不是 `NOT IN (SELECT order_id FROM tasks)`：
        后者在子查询返回 NULL 时的语义容易踩坑，MySQL 对它的优化通常也不如反连接。

        `statuses` **默认 None = 不过滤**，保持 Phase 3 手动路径
        （`scripts/gen_tasks.py`）的既有行为不变。传 `[ANALYZED]` 时只捡
        「已分析但还没建任务」的单 —— AI 分析与补偿（L3）走这条路，避免把
        `IMPORTED` 的单也建出任务、绕过了分析。
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
        if statuses:
            stmt = stmt.where(Order.status.in_([s.value for s in statuses]))

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
