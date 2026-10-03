"""看板的响应模型 —— 见《API接口设计》§11。字段名严格对齐文档，不加不减。"""

from datetime import date, datetime

from pydantic import BaseModel, Field


class DashboardOrderStats(BaseModel):
    total: int
    today: int = Field(description="今天导入的订单数，按 orders.imported_at 算")
    by_status: dict[str, int] = Field(
        description="各订单状态的条数；**缺失的状态补 0**，保证键集固定"
    )


class DashboardTaskStats(BaseModel):
    total: int
    by_status: dict[str, int]
    by_priority: dict[str, int]


class DashboardRiskStats(BaseModel):
    """风险计数 —— 口径是「每单最新一条分析」，不是分析表全表计数。"""

    high: int
    medium: int
    need_contact: int


class DashboardRpaStats(BaseModel):
    workers_online: int = Field(description="最近 2 分钟内有心跳的 claimed_by 去重数")
    last_success_at: datetime | None = None


class DashboardSummary(BaseModel):
    orders: DashboardOrderStats
    tasks: DashboardTaskStats
    risk: DashboardRiskStats
    rpa: DashboardRpaStats


class DashboardTrendPoint(BaseModel):
    date: date
    imported: int
    success: int
    failed: int


class DashboardTrends(BaseModel):
    days: list[DashboardTrendPoint] = Field(
        description="连续 N 天，缺数据的日期补 0，从早到晚"
    )
