"""看板服务 —— 把仓储的分组计数组装成 §11 的形状。

服务层在这里做两件仓储不做的事：
  · **补零**：把枚举里存在、但库里一条没有的状态补成 0，让 `by_status` 的
    键集固定。前端画卡片/表头时不必再自己兜底，接口契约也更稳。
  · **补日期**：趋势要的是「连续 N 天」，库里只有「有数据的那几天」，
    中间空档必须补 0，否则折线图会把两段不连续的日期画成相邻的。
"""

from datetime import date, datetime, time, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import OrderStatus, Priority, TaskStatus
from app.repositories.dashboard_repository import DashboardRepository
from app.schemas.dashboard import (
    DashboardOrderStats,
    DashboardRiskStats,
    DashboardRpaStats,
    DashboardSummary,
    DashboardTaskStats,
    DashboardTrendPoint,
    DashboardTrends,
)

#: Worker 在线的判定窗口：心跳超过这个时长没刷就认为它挂了 ——
#: 与僵尸回收用的是同一套「心跳」语义（《数据库设计》§6.1）。
WORKER_ONLINE_WINDOW = timedelta(minutes=2)

#: 趋势天数的合法区间与默认值 —— 与《API接口设计》§11.2 的 `days=7` 对齐。
TREND_MIN_DAYS = 1
TREND_MAX_DAYS = 30
TREND_DEFAULT_DAYS = 7


class DashboardService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = DashboardRepository(session)

    async def summary(self) -> DashboardSummary:
        now = datetime.now()
        today_start = datetime.combine(now.date(), time.min)
        tomorrow_start = today_start + timedelta(days=1)

        orders_total = await self.repo.count_orders()
        orders_today = await self.repo.count_orders_imported_between(
            today_start, tomorrow_start
        )
        orders_by_status = await self.repo.count_orders_by_status()

        tasks_total = await self.repo.count_tasks()
        tasks_by_status = await self.repo.count_tasks_by_status()
        tasks_by_priority = await self.repo.count_tasks_by_priority()

        risk = await self.repo.count_risk()
        workers_online = await self.repo.count_workers_online(now - WORKER_ONLINE_WINDOW)
        last_success_at = await self.repo.last_success_at()

        return DashboardSummary(
            orders=DashboardOrderStats(
                total=orders_total,
                today=orders_today,
                by_status=self._fill(orders_by_status, OrderStatus),
            ),
            tasks=DashboardTaskStats(
                total=tasks_total,
                by_status=self._fill(tasks_by_status, TaskStatus),
                by_priority=self._fill(tasks_by_priority, Priority),
            ),
            risk=DashboardRiskStats(
                high=risk.high, medium=risk.medium, need_contact=risk.need_contact
            ),
            rpa=DashboardRpaStats(
                workers_online=workers_online, last_success_at=last_success_at
            ),
        )

    async def trends(self, days: int) -> DashboardTrends:
        # 再夹一次是**防御性**的：接口层已经拦了越界并返回 4000，
        # 但 service 也可能被脚本/其他服务直接调用，不能假设调用方守规矩。
        days = max(TREND_MIN_DAYS, min(TREND_MAX_DAYS, days))

        today = date.today()
        first_day = today - timedelta(days=days - 1)
        start = datetime.combine(first_day, time.min)
        end = datetime.combine(today, time.min) + timedelta(days=1)

        imported = await self.repo.trend_imported(start, end)
        tasks = await self.repo.trend_tasks(start, end)

        points = [
            DashboardTrendPoint(
                date=day,
                imported=imported.get(day, 0),
                success=tasks.get(day, {}).get(TaskStatus.SUCCESS.value, 0),
                failed=tasks.get(day, {}).get(TaskStatus.FAILED.value, 0),
            )
            for day in (first_day + timedelta(days=i) for i in range(days))
        ]
        return DashboardTrends(days=points)

    @staticmethod
    def _fill(counts: dict[str, int], enum_cls) -> dict[str, int]:
        """按枚举全集补 0 —— 库里有但枚举没有的取值也保留，宁可多看一眼。"""
        filled = {member.value: counts.get(member.value, 0) for member in enum_cls}
        for key, value in counts.items():
            if key not in filled:
                filled[key] = value
        return filled
