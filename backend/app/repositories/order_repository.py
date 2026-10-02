"""orders 表的数据访问，含「取每单最新一次 AI 分析」这个核心查询。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import OrderStatus
from app.models.ai_analysis import AiAnalysis
from app.models.ai_review_log import AiReviewLog
from app.models.order import Order
from app.models.task import Task
from app.models.user import User
from app.repositories.base import BaseRepository
from app.schemas.common import PageParams

#: 视为「有风险」的等级。LOW 不算，否则筛选等于没筛。
RISKY_LEVELS = ("MEDIUM", "HIGH")


def latest_analysis_id_subquery():
    """「每个订单最新一次分析」的相关子查询。

    这是 SQL 里的经典「每组取最新一条」问题。用相关子查询而不是
    `GROUP BY + MAX(created_at)` 再 JOIN 回去，是因为后者在
    created_at 有重复时会一次匹配出多行，行数悄悄膨胀。
    子查询只返回一个 id，天然不会重复。

    走的是 `idx_ai_analyses_order (order_id, created_at)` 索引。

    放在模块级而不是 OrderRepository 的方法里：任务仓储生成任务时
    也要取「这一单的最新分析」，两处必须共用同一套「最新」的定义，
    否则订单列表显示的分析和任务依据的分析可能不是同一条。
    """
    return (
        select(AiAnalysis.id)
        .where(AiAnalysis.order_id == Order.id)
        .order_by(AiAnalysis.created_at.desc(), AiAnalysis.id.desc())
        .limit(1)
        .correlate(Order)
        .scalar_subquery()
    )


@dataclass(slots=True)
class OrderListRow:
    """订单列表一行。

    用显式的 dataclass 而不是裸 Row，是为了让 service 拿到的字段名固定 ——
    直接传 Row 的话，字段名藏在 SQL 的 label 里，改个查询就可能静默丢掉一列。
    """

    order: Order
    risk_level: str | None
    priority: str | None
    task_id: int | None
    task_status: str | None


@dataclass(slots=True)
class OrderFilters:
    """订单列表的筛选条件。放在仓储文件里，因为它本质是查询条件的载体。"""

    statuses: Sequence[str] | None = None
    platform: str | None = None
    order_no: str | None = None
    customer_name: str | None = None
    has_risk: bool = False
    start_date: date | None = None
    end_date: date | None = None


class OrderRepository(BaseRepository[Order]):
    model = Order

    # ---------- 导入相关 ----------

    async def find_existing_order_nos(self, order_nos: Sequence[str]) -> set[str]:
        """一次查回**所有**已存在的订单号。

        逐行查会变成 N 次往返；一次 `IN` 查回来，几百行的文件也只是几个网络来回。

        注意它的角色：**这只是一个「预检」，用来生成好看的错误清单，
        不是正确性的依据**。真正的唯一性由 `uk_orders_order_no` 唯一索引保证 ——
        预检和插入之间存在时间窗，并发导入时预检可能都说「不存在」，
        那时靠唯一索引兜底。见 import_service 里对 IntegrityError 的处理。
        """
        if not order_nos:
            return set()
        rows = await self.session.scalars(
            select(Order.order_no).where(Order.order_no.in_(list(order_nos)))
        )
        return set(rows)

    def add_all(self, rows: Sequence[dict[str, Any]]) -> None:
        """批量挂载，不提交。

        用 `add_all` 而不是循环 `add`：SQLAlchemy 仍然会逐条 INSERT，
        但省掉了每次 `add` 的对象状态检查开销。真正的大批量导入应该上
        `insert().values([...])` 多值语法，v1 的几千行用不着那一层优化。
        """
        self.session.add_all([Order(**row) for row in rows])

    # ---------- AI 分析流水线：状态扫描与 CAS ----------

    async def list_by_status_older_than(
        self, status: OrderStatus, cutoff: datetime
    ) -> list[Order]:
        """「停在 `status`、且最后一次状态变更早于 `cutoff`」的订单。

        这是 AI 补偿扫描的取数口，两条腿共用，只是参数不同：
          · L1  `IMPORTED`  + 入队宽限截止 → 「写库成功、入队失败」的漏网单
          · L2  `ANALYZING` + 分析超时截止 → 「分析进程中途死了」的卡死单

        形状与 `TaskRepository.list_stale_running` 一致：**时间边界由调用方算**
        （`ai_reconciler` 里的 `*_cutoff()`），仓储只管「扫哪些行」。这样测这个
        函数不必 sleep，传一个固定 cutoff 就行。

        判据用 `updated_at` 而不是 `created_at`：它由 DDL 的
        `ON UPDATE CURRENT_TIMESTAMP` 维护（database/schema/003_orders.sql），
        **状态一变就刷新**，因此等价于「最后一次状态变更时刻」。改用 created_at
        的话，一张导入很久、刚被置回 IMPORTED 的单会被立刻判成陈旧而反复重推。

        走 `idx_orders_status`（status 是前导列），每分钟扫一次、命中通常为 0。
        """
        stmt = select(Order).where(Order.status == status, Order.updated_at < cutoff)
        return list(await self.session.scalars(stmt))

    async def cas_transition_status(
        self, order_id: int, from_status: OrderStatus, to_status: OrderStatus
    ) -> bool:
        """**条件**状态推进：仅当订单当前处于 `from_status` 才改成 `to_status`，
        返回到底改没改到（`rowcount > 0`）。

        为什么需要它 —— 消费端幂等。AI 分析队列是**至少一次投递**：ZADD 覆盖
        保证队列里不堆重复项，但挡不住「同一单被投两次」的时序，例如：

            worker A 出队取到 order 7
            → A 分析到一半挂了，L2 把 7 置回 IMPORTED 并重新入队
            → worker B 也取到 7，于是两个 worker 同时分析同一单

        条件 UPDATE 让后到的那个 `rowcount == 0`，调用方直接丢弃并记一条日志。

        它是 `TaskService.pop_next_runnable`「出队后回查一次数据库」的**无竞态
        版本**：回查是「先读、再写」，两步之间（TOCTOU）状态可能又变了；而条件
        UPDATE 把判断和写入压进同一条语句，由 MySQL 的行锁保证原子。

        不提交 —— 事务边界归 service（与 `_set_order_status`、
        `RpaService.reap_stale` 同一条原则：先写库、后动队列，两者本来就不在
        同一个事务里）。

        默认的 `synchronize_session="auto"` 在这里会走 `evaluate`：条件是
        id 与 status 的等值比较，SQLAlchemy 能算出被改的是哪些已加载对象，
        顺手把会话里那行同步成 `to_status`，免得同一事务后续读到旧值。
        """
        result = await self.session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == from_status)
            .values(status=to_status)
        )
        return result.rowcount > 0

    # ---------- 列表 ----------

    def _list_stmt(self, filters: OrderFilters) -> Select:
        stmt = (
            select(
                Order,
                AiAnalysis.risk_level,
                AiAnalysis.priority,
                Task.id.label("task_id"),
                Task.status.label("task_status"),
            )
            .outerjoin(AiAnalysis, AiAnalysis.id == latest_analysis_id_subquery())
            .outerjoin(Task, Task.order_id == Order.id)
        )

        conditions = []
        if filters.statuses:
            conditions.append(Order.status.in_(list(filters.statuses)))
        if filters.platform:
            conditions.append(Order.platform == filters.platform)
        if filters.order_no:
            conditions.append(Order.order_no == filters.order_no)
        if filters.customer_name:
            # 需求只要求前缀匹配（《API接口设计》§6.1），不是全文检索：
            # `LIKE '张%'` 能走索引，`LIKE '%张%'` 不能。
            conditions.append(Order.customer_name.like(f"{filters.customer_name}%"))
        if filters.has_risk:
            conditions.append(AiAnalysis.risk_level.in_(RISKY_LEVELS))
        if filters.start_date:
            conditions.append(Order.ordered_at >= datetime.combine(filters.start_date, datetime.min.time()))
        if filters.end_date:
            # 用 `< 次日零点` 而不是 `<= 当日 23:59:59`：后者会漏掉
            # 23:59:59.5 这种带毫秒的时间（列类型是 DATETIME，毫秒会被截断，
            # 但换成 DATETIME(3) 就会漏），前者的边界是封闭且不依赖精度的。
            conditions.append(
                Order.ordered_at
                < datetime.combine(filters.end_date, datetime.min.time()) + timedelta(days=1)
            )

        if conditions:
            stmt = stmt.where(and_(*conditions))

        # 默认排序按 created_at DESC；再加 id DESC 是为了让同一秒内导入的订单
        # 有稳定顺序 —— 否则翻页时可能出现「第 1 页和第 2 页看到同一条」。
        return stmt.order_by(Order.created_at.desc(), Order.id.desc())

    async def list_orders(
        self, filters: OrderFilters, params: PageParams
    ) -> tuple[list[OrderListRow], int]:
        stmt = self._list_stmt(filters)
        # 这里的 JOIN 是 1:1 的（分析取最新一条、任务一单一任务），
        # 所以「结果行数 == 订单数」，基类默认的计数方式是对的。
        rows, total = await self.paginate(stmt, params)
        return [
            OrderListRow(
                order=row[0],
                risk_level=row[1],
                priority=row[2],
                task_id=row[3],
                task_status=row[4],
            )
            for row in rows
        ], total

    # ---------- 详情 ----------

    async def get_with_analysis_and_task(
        self, order_id: int
    ) -> tuple[Order, AiAnalysis | None, Task | None] | None:
        """详情页一次取回订单 + 最新分析 + 任务。

        需求里详情页还要一次返回修正记录，但那是一对多，混进这个查询会让
        上面「1:1 所以行数不膨胀」的前提失效，所以另外走一次查询。
        """
        stmt = (
            select(Order, AiAnalysis, Task)
            .outerjoin(AiAnalysis, AiAnalysis.id == latest_analysis_id_subquery())
            .outerjoin(Task, Task.order_id == Order.id)
            .where(Order.id == order_id)
        )
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None
        return row[0], row[1], row[2]

    async def list_review_logs(self, order_id: int) -> list[tuple[AiReviewLog, str]]:
        """人工修正记录，附带修正人用户名。

        日志表只存 reviewer 的 id，但接口要给前端显示人 —— 与其让前端
        再查一次用户表，不如在这里 JOIN 掉。
        """
        stmt = (
            select(AiReviewLog, User.username)
            .join(User, User.id == AiReviewLog.reviewer)
            .where(AiReviewLog.order_id == order_id)
            .order_by(AiReviewLog.reviewed_at.desc(), AiReviewLog.id.desc())
        )
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]

