"""看板的聚合查询 —— 见《API接口设计》§11。

看板不做「统计表」，所有数字都从既有的列现算：订单按 `status`、
任务按 `status`/`priority` 分组计数、风险按「每单最新一条分析」去重。
这些列都有索引（`idx_orders_status` / `idx_tasks_status_priority` /
`idx_ai_analyses_order`），命中量是分组数不是行数，几分钟刷一次没有压力。

关于「最新」的定义：这里**复用** `order_repository.latest_analysis_id_subquery`，
而不是自己写一个 `MAX(id)`。两处的定义必须一致，否则看板上的高风险数与
订单列表里实际标出来的高风险会对不上，而这种偏差最难排查。
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select

from app.core.enums import RiskLevel, TaskStatus
from app.models.ai_analysis import AiAnalysis
from app.models.order import Order
from app.models.task import Task
from app.repositories.base import BaseRepository
from app.repositories.order_repository import latest_analysis_id_subquery


def _as_date(value: Any) -> date:
    """把 `func.date(...)` 的结果归一到 `date`。

    MySQL 的 `DATE()` 返回的是 `date`，但经某些驱动/方言可能给 `str` 或
    `datetime`。趋势的补零靠日期做 key，这里不归一的话会出现
    「明明有数据却补成了 0」这种只在特定驱动下才现形的错。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


@dataclass(slots=True)
class RiskCounts:
    """风险计数 —— 三个数分开给，因为它们的口径不同。"""

    high: int
    medium: int
    need_contact: int


class DashboardRepository(BaseRepository[Order]):
    """看板取数。

    刻意**继承 Order 而不是新建一张无主表**：这里没有属于自己的表，
    所有查询都从既有模型出发；挑 Order 只是给基类一个 `model`，
    真正用到的 `add` / `get` 一个都不会走。
    """

    model = Order

    # ---------- 订单 ----------

    async def count_orders(self) -> int:
        return int(await self.session.scalar(select(func.count()).select_from(Order)) or 0)

    async def count_orders_imported_between(self, start: datetime, end: datetime) -> int:
        """`imported_at ∈ [start, end)` 的订单数 —— 「今天导入了多少单」。

        用半开区间而不是 `func.date(imported_at) == today`：后者对列做了函数
        变换，`idx_orders_imported_at` 用不上，会退化成全表扫。区间写法两边
        都是常量，索引直接命中。end 由调用方给成「次日零点」。
        """
        stmt = select(func.count()).select_from(Order).where(
            Order.imported_at >= start, Order.imported_at < end
        )
        return int(await self.session.scalar(stmt) or 0)

    async def count_orders_by_status(self) -> dict[str, int]:
        rows = await self.session.execute(
            select(Order.status, func.count()).group_by(Order.status)
        )
        return {status: int(n) for status, n in rows.all()}

    # ---------- 任务 ----------

    async def count_tasks(self) -> int:
        return int(await self.session.scalar(select(func.count()).select_from(Task)) or 0)

    async def count_tasks_by_status(self) -> dict[str, int]:
        rows = await self.session.execute(
            select(Task.status, func.count()).group_by(Task.status)
        )
        return {status: int(n) for status, n in rows.all()}

    async def count_tasks_by_priority(self) -> dict[str, int]:
        rows = await self.session.execute(
            select(Task.priority, func.count()).group_by(Task.priority)
        )
        return {priority: int(n) for priority, n in rows.all()}

    # ---------- 风险（去重到每单最新一条分析）----------

    async def count_risk(self) -> RiskCounts:
        """高风险 / 中风险 / 需联系客户 —— **只算每单最新一条分析**。

        一单可以有多条分析（重新分析会追加），直接把 `ai_analyses` 全表计数
        会把同一单重复算，数字虚高。所以 JOIN 条件是「这条就是该单的最新一条」。
        """
        risk_rows = await self.session.execute(
            select(AiAnalysis.risk_level, func.count())
            .select_from(Order)
            .join(AiAnalysis, AiAnalysis.id == latest_analysis_id_subquery())
            .group_by(AiAnalysis.risk_level)
        )
        by_level = {level: int(n) for level, n in risk_rows.all() if level is not None}

        need_contact = await self.session.scalar(
            select(func.count())
            .select_from(Order)
            .join(AiAnalysis, AiAnalysis.id == latest_analysis_id_subquery())
            .where(AiAnalysis.need_contact.is_(True))
        )
        return RiskCounts(
            high=by_level.get(RiskLevel.HIGH.value, 0),
            medium=by_level.get(RiskLevel.MEDIUM.value, 0),
            need_contact=int(need_contact or 0),
        )

    # ---------- RPA ----------

    async def count_workers_online(self, active_since: datetime) -> int:
        """最近 `active_since` 之后有心跳的 Worker 数（按 `claimed_by` 去重）。

        不维护「在线 Worker 表」：`tasks.heartbeat_at` 本来就是为僵尸回收写的，
        顺手拿来当在线判据，少一张表、少一处不一致。
        """
        stmt = select(func.count(func.distinct(Task.claimed_by))).where(
            Task.claimed_by.is_not(None), Task.heartbeat_at > active_since
        )
        return int(await self.session.scalar(stmt) or 0)

    async def last_success_at(self) -> datetime | None:
        return await self.session.scalar(
            select(func.max(Task.finished_at)).where(Task.status == TaskStatus.SUCCESS.value)
        )

    # ---------- 趋势 ----------
    #
    # 诚实口径（同样写在这里，免得以后有人当成 bug）：
    #   · `imported` 按 `orders.imported_at` 归到当天；
    #   · `success` / `failed` 按 `tasks.finished_at` 归到**到达终态那天**。
    # 系统**没有状态变更历史表**，所以「某天有多少任务成功」实际是「某天有多少
    # 任务跑到失败/成功这个终点」，重试不单独体现 —— 不为画一条趋势图再加一张表。

    async def trend_imported(self, start: datetime, end: datetime) -> dict[date, int]:
        rows = await self.session.execute(
            select(func.date(Order.imported_at), func.count())
            .where(Order.imported_at >= start, Order.imported_at < end)
            .group_by(func.date(Order.imported_at))
        )
        return {_as_date(day): int(n) for day, n in rows.all()}

    async def trend_tasks(
        self, start: datetime, end: datetime
    ) -> dict[date, dict[str, int]]:
        """`{日期: {"SUCCESS": n, "FAILED": m}}`，只统计两个终态。"""
        rows = await self.session.execute(
            select(func.date(Task.finished_at), Task.status, func.count())
            .where(
                Task.finished_at.is_not(None),
                Task.finished_at >= start,
                Task.finished_at < end,
                Task.status.in_([TaskStatus.SUCCESS.value, TaskStatus.FAILED.value]),
            )
            .group_by(func.date(Task.finished_at), Task.status)
        )
        out: dict[date, dict[str, int]] = {}
        for day, status, n in rows.all():
            out.setdefault(_as_date(day), {})[status] = int(n)
        return out
