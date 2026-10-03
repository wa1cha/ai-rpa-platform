"""看板接口 —— 见《API接口设计》§11。两个端点都是管理员权限。

看板是**只读**的聚合视图，写操作一概没有：数字都从既有列现算，
所以这里连一个 `POST` 都不需要。
"""

from fastapi import APIRouter, Query

from app.api.deps import AdminUser, SessionDep
from app.core.exceptions import ParamError
from app.schemas.common import ApiResponse
from app.schemas.dashboard import DashboardSummary, DashboardTrends
from app.services.dashboard_service import (
    TREND_DEFAULT_DAYS,
    TREND_MAX_DAYS,
    TREND_MIN_DAYS,
    DashboardService,
)

router = APIRouter(prefix="/dashboard", tags=["看板"])


@router.get(
    "/summary",
    response_model=ApiResponse[DashboardSummary],
    summary="汇总统计",
    description="订单/任务/风险/RPA 四组计数。风险口径为『每单最新一条分析』。",
)
async def get_summary(admin: AdminUser, session: SessionDep) -> ApiResponse[DashboardSummary]:
    return ApiResponse.ok(await DashboardService(session).summary())


@router.get(
    "/trends",
    response_model=ApiResponse[DashboardTrends],
    summary="趋势",
    description="最近 N 天（1..30，默认 7）的导入/成功/失败按天计数，缺日补 0。",
)
async def get_trends(
    admin: AdminUser,
    session: SessionDep,
    days: int = Query(default=TREND_DEFAULT_DAYS, description="统计天数，1..30"),
) -> ApiResponse[DashboardTrends]:
    if not TREND_MIN_DAYS <= days <= TREND_MAX_DAYS:
        raise ParamError(
            f"days 必须在 {TREND_MIN_DAYS} 到 {TREND_MAX_DAYS} 之间，收到 {days}"
        )
    return ApiResponse.ok(await DashboardService(session).trends(days))
